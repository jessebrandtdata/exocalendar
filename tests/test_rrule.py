"""RRULE engine tests: RFC 5545 §3.8.5.3 examples with the RFC's listed outputs."""

from datetime import date, datetime

import pytest

from exocalendar.ical import Component, TZResolver
from exocalendar.rrule import Occurrence, RRule, RRuleError, expand

NY = "America/New_York"


def take(rule: str, dtstart: datetime | date, n: int) -> list:
    it = RRule.parse(rule).iterate(dtstart)
    out = []
    for _ in range(n):
        try:
            out.append(next(it))
        except StopIteration:
            break
    return out


def dts(*specs: str) -> list[datetime]:
    return [datetime.strptime(s, "%Y%m%dT%H%M%S") for s in specs]


D = datetime(1997, 9, 2, 9, 0)  # the RFC's canonical DTSTART (naive here)


def test_daily_count_10():
    got = take("FREQ=DAILY;COUNT=10", D, 20)
    assert got == [datetime(1997, 9, d, 9, 0) for d in range(2, 12)]


def test_daily_until():
    got = take("FREQ=DAILY;UNTIL=19970905T090000", D, 20)
    assert got == [datetime(1997, 9, d, 9, 0) for d in (2, 3, 4, 5)]


def test_daily_interval_2():
    got = take("FREQ=DAILY;INTERVAL=2", D, 4)
    assert got == [datetime(1997, 9, d, 9, 0) for d in (2, 4, 6, 8)]


def test_daily_interval_10_count_5():
    got = take("FREQ=DAILY;INTERVAL=10;COUNT=5", D, 10)
    assert got == dts(
        "19970902T090000", "19970912T090000", "19970922T090000",
        "19971002T090000", "19971012T090000",
    )


def test_weekly_count_10():
    got = take("FREQ=WEEKLY;COUNT=10", D, 20)
    assert got[:4] == dts(
        "19970902T090000", "19970909T090000", "19970916T090000", "19970923T090000"
    )
    assert len(got) == 10


def test_weekly_byday_until():
    got = take("FREQ=WEEKLY;UNTIL=19971007T000000;BYDAY=TU,TH", D, 30)
    assert got == dts(
        "19970902T090000", "19970904T090000", "19970909T090000", "19970911T090000",
        "19970916T090000", "19970918T090000", "19970923T090000", "19970925T090000",
        "19970930T090000", "19971002T090000",
    )


def test_wkst_mo_vs_su():
    """The RFC's WKST demonstration pair."""
    d = datetime(1997, 8, 5, 9, 0)
    mo = take("FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=MO", d, 10)
    su = take("FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=SU", d, 10)
    assert mo == dts("19970805T090000", "19970810T090000", "19970819T090000", "19970824T090000")
    assert su == dts("19970805T090000", "19970817T090000", "19970819T090000", "19970831T090000")


def test_monthly_first_friday():
    got = take("FREQ=MONTHLY;COUNT=10;BYDAY=1FR", datetime(1997, 9, 5, 9, 0), 20)
    assert got == dts(
        "19970905T090000", "19971003T090000", "19971107T090000", "19971205T090000",
        "19980102T090000", "19980206T090000", "19980306T090000", "19980403T090000",
        "19980501T090000", "19980605T090000",
    )


def test_monthly_first_and_last_sunday_every_other_month():
    got = take(
        "FREQ=MONTHLY;INTERVAL=2;COUNT=10;BYDAY=1SU,-1SU", datetime(1997, 9, 7, 9, 0), 20
    )
    assert got == dts(
        "19970907T090000", "19970928T090000", "19971102T090000", "19971130T090000",
        "19980104T090000", "19980125T090000", "19980301T090000", "19980329T090000",
        "19980503T090000", "19980531T090000",
    )


def test_monthly_second_to_last_monday():
    got = take("FREQ=MONTHLY;COUNT=6;BYDAY=-2MO", datetime(1997, 9, 22, 9, 0), 10)
    assert got == dts(
        "19970922T090000", "19971020T090000", "19971117T090000",
        "19971222T090000", "19980119T090000", "19980216T090000",
    )


def test_monthly_third_to_last_day():
    got = take("FREQ=MONTHLY;BYMONTHDAY=-3", datetime(1997, 9, 28, 9, 0), 6)
    assert got == dts(
        "19970928T090000", "19971029T090000", "19971128T090000",
        "19971229T090000", "19980129T090000", "19980226T090000",
    )


def test_monthly_bymonthday_2_and_15():
    got = take("FREQ=MONTHLY;COUNT=10;BYMONTHDAY=2,15", D, 20)
    assert got == dts(
        "19970902T090000", "19970915T090000", "19971002T090000", "19971015T090000",
        "19971102T090000", "19971115T090000", "19971202T090000", "19971215T090000",
        "19980102T090000", "19980115T090000",
    )


def test_monthly_first_and_last_day():
    # DTSTART Sep 2: Sep 1 precedes it, so the series starts Sep 30
    got = take("FREQ=MONTHLY;COUNT=10;BYMONTHDAY=1,-1", D, 20)
    assert got == dts(
        "19970930T090000", "19971001T090000", "19971031T090000", "19971101T090000",
        "19971130T090000", "19971201T090000", "19971231T090000", "19980101T090000",
        "19980131T090000", "19980201T090000",
    )


def test_monthly_interval_18_bymonthday_10_to_15():
    got = take(
        "FREQ=MONTHLY;INTERVAL=18;COUNT=10;BYMONTHDAY=10,11,12,13,14,15",
        datetime(1997, 9, 10, 9, 0), 20,
    )
    assert got == dts(
        "19970910T090000", "19970911T090000", "19970912T090000", "19970913T090000",
        "19970914T090000", "19970915T090000", "19990310T090000", "19990311T090000",
        "19990312T090000", "19990313T090000",
    )


def test_yearly_bymonth_6_7():
    got = take("FREQ=YEARLY;COUNT=10;BYMONTH=6,7", datetime(1997, 6, 10, 9, 0), 20)
    assert got == dts(
        "19970610T090000", "19970710T090000", "19980610T090000", "19980710T090000",
        "19990610T090000", "19990710T090000", "20000610T090000", "20000710T090000",
        "20010610T090000", "20010710T090000",
    )


def test_yearly_interval_3_byyearday():
    got = take(
        "FREQ=YEARLY;INTERVAL=3;COUNT=10;BYYEARDAY=1,100,200", datetime(1997, 1, 1, 9, 0), 20
    )
    assert got == dts(
        "19970101T090000", "19970410T090000", "19970719T090000",
        "20000101T090000", "20000409T090000", "20000718T090000",
        "20030101T090000", "20030410T090000", "20030719T090000",
        "20060101T090000",
    )


def test_yearly_20th_monday():
    got = take("FREQ=YEARLY;BYDAY=20MO", datetime(1997, 5, 19, 9, 0), 3)
    assert got == dts("19970519T090000", "19980518T090000", "19990517T090000")


def test_yearly_byweekno_20_monday():
    got = take("FREQ=YEARLY;BYWEEKNO=20;BYDAY=MO", datetime(1997, 5, 12, 9, 0), 3)
    assert got == dts("19970512T090000", "19980511T090000", "19990517T090000")


def test_byweekno_year_boundary_follows_iso():
    # Jan 1-2 2022 belong to week 52 of 2021 (date(2022,1,1).isocalendar()
    # agrees); python-dateutil mis-numbers this year and calls it week 53
    got = take("FREQ=YEARLY;BYWEEKNO=52;COUNT=9", datetime(2021, 12, 20, 9, 0), 12)
    assert got == dts(
        "20211227T090000", "20211228T090000", "20211229T090000", "20211230T090000",
        "20211231T090000", "20220101T090000", "20220102T090000",
        "20221226T090000", "20221227T090000",
    )


def test_yearly_thursdays_in_march():
    got = take("FREQ=YEARLY;BYMONTH=3;BYDAY=TH", datetime(1997, 3, 13, 9, 0), 11)
    assert got == dts(
        "19970313T090000", "19970320T090000", "19970327T090000",
        "19980305T090000", "19980312T090000", "19980319T090000", "19980326T090000",
        "19990304T090000", "19990311T090000", "19990318T090000", "19990325T090000",
    )


def test_friday_the_13th():
    got = take("FREQ=MONTHLY;BYDAY=FR;BYMONTHDAY=13", D, 5)
    assert got == dts(
        "19980213T090000", "19980313T090000", "19981113T090000",
        "19990813T090000", "20001013T090000",
    )


def test_saturday_following_first_sunday():
    got = take(
        "FREQ=MONTHLY;BYDAY=SA;BYMONTHDAY=7,8,9,10,11,12,13",
        datetime(1997, 9, 13, 9, 0), 7,
    )
    assert got == dts(
        "19970913T090000", "19971011T090000", "19971108T090000", "19971213T090000",
        "19980110T090000", "19980207T090000", "19980307T090000",
    )


def test_us_election_day():
    got = take(
        "FREQ=YEARLY;INTERVAL=4;BYMONTH=11;BYDAY=TU;BYMONTHDAY=2,3,4,5,6,7,8",
        datetime(1996, 11, 5, 9, 0), 3,
    )
    assert got == dts("19961105T090000", "20001107T090000", "20041102T090000")


def test_bysetpos_third_tu_we_th():
    got = take(
        "FREQ=MONTHLY;COUNT=3;BYDAY=TU,WE,TH;BYSETPOS=3", datetime(1997, 9, 4, 9, 0), 10
    )
    assert got == dts("19970904T090000", "19971007T090000", "19971106T090000")


def test_bysetpos_second_to_last_weekday():
    got = take(
        "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-2", datetime(1997, 9, 29, 9, 0), 7
    )
    assert got == dts(
        "19970929T090000", "19971030T090000", "19971127T090000", "19971230T090000",
        "19980129T090000", "19980226T090000", "19980330T090000",
    )


def test_byday_mixed_ordinal_and_plain_is_a_union():
    # RFC 5545: "3TH,FR" means the 3rd Thursday AND every Friday (a union).
    # (python-dateutil intersects these and yields nothing — engine deviates
    # from the oracle here on purpose; the oracle generator never mixes.)
    got = take("FREQ=MONTHLY;COUNT=6;BYMONTH=7;BYDAY=3TH,FR", datetime(2013, 7, 5, 8, 15), 10)
    assert got == dts(
        "20130705T081500", "20130712T081500", "20130718T081500",
        "20130719T081500", "20130726T081500", "20140704T081500",
    )


def test_hourly_interval_3_until():
    got = take("FREQ=HOURLY;INTERVAL=3;UNTIL=19970902T170000", D, 10)
    assert got == dts("19970902T090000", "19970902T120000", "19970902T150000")


def test_minutely_interval_15_count_6():
    got = take("FREQ=MINUTELY;INTERVAL=15;COUNT=6", D, 10)
    assert got == dts(
        "19970902T090000", "19970902T091500", "19970902T093000",
        "19970902T094500", "19970902T100000", "19970902T101500",
    )


def test_minutely_interval_90_count_4():
    got = take("FREQ=MINUTELY;INTERVAL=90;COUNT=4", D, 10)
    assert got == dts(
        "19970902T090000", "19970902T103000", "19970902T120000", "19970902T133000"
    )


def test_daily_byhour_byminute():
    got = take("FREQ=DAILY;BYHOUR=9,10,11;BYMINUTE=0,30;COUNT=8", D, 10)
    assert got == dts(
        "19970902T090000", "19970902T093000", "19970902T100000", "19970902T103000",
        "19970902T110000", "19970902T113000", "19970903T090000", "19970903T093000",
    )


def test_invalid_dates_skipped():
    got = take("FREQ=MONTHLY;BYMONTHDAY=15,30;COUNT=5", datetime(2007, 1, 15, 9, 0), 10)
    assert got == [
        datetime(2007, 1, 15, 9, 0), datetime(2007, 1, 30, 9, 0),
        datetime(2007, 2, 15, 9, 0),
        datetime(2007, 3, 15, 9, 0), datetime(2007, 3, 30, 9, 0),
    ]


def test_date_mode():
    got = take("FREQ=WEEKLY;COUNT=3", date(2026, 1, 5), 5)
    assert got == [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19)]


def test_overflow_guard():
    from exocalendar.rrule import RRuleOverflow

    with pytest.raises(RRuleOverflow):
        take("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30", D, 1)


# --- parse validation --------------------------------------------------------

def test_parse_rejects_garbage():
    for bad in [
        "", "FREQ=FORTNIGHTLY", "COUNT=3", "FREQ=DAILY;NONSENSE=1",
        "FREQ=DAILY;COUNT=x", "FREQ=DAILY;BYDAY=XX", "FREQ=DAILY;COUNT=1;UNTIL=20260101T000000Z",
        "FREQ=DAILY;BYSETPOS=2",
    ]:
        with pytest.raises(RRuleError):
            RRule.parse(bad)


def test_parse_accepts_all_rfc_parts():
    r = RRule.parse(
        "FREQ=YEARLY;INTERVAL=2;BYSECOND=0;BYMINUTE=30;BYHOUR=8,9;"
        "BYDAY=SU;BYMONTHDAY=7;BYMONTH=1;BYSETPOS=1;WKST=SU;COUNT=3"
    )
    assert r is not None


# --- expand(): EXDATE / RDATE / overrides ------------------------------------

def _cal(text: str) -> Component:
    return Component.parse(text)


def _expand_ics(text: str, start: datetime, end: datetime) -> list[Occurrence]:
    cal = _cal(text)
    tz = TZResolver.from_calendar(cal)
    return expand(cal.find_children("VEVENT"), tz, start, end)


APPLE = (Path := __import__("pathlib").Path)(__file__).parent / "corpus" / "apple-recurring-override.ics"


def test_expand_daily_with_exdate_and_override():
    from datetime import timezone as _tz

    text = APPLE.read_text()
    occs = _expand_ics(
        text,
        datetime(2026, 1, 1, tzinfo=_tz.utc),
        datetime(2026, 2, 28, tzinfo=_tz.utc),
    )
    starts = {o.start.value.strftime("%Y%m%dT%H%M%S") for o in occs}
    # daily Jan 12 .. Jan 30 (UNTIL 09:30Z == 10:30 Berlin)
    assert "20260112T103000" in starts
    assert "20260130T103000" in starts
    # EXDATE Jan 19 removed
    assert "20260119T103000" not in starts
    # override moved Jan 21 10:30 -> 11:30
    assert "20260121T103000" not in starts
    assert "20260121T113000" in starts
    moved = [o for o in occs if o.start.value.strftime("%Y%m%dT%H%M%S") == "20260121T113000"]
    assert moved[0].component.get("SUMMARY").value == "Standup (moved)"
    assert moved[0].recurrence_id is not None
    # total: Jan 12..30 = 19 days, minus 1 exdate = 18
    assert len(occs) == 18


def test_expand_range_filters():
    from datetime import timezone as _tz

    text = APPLE.read_text()
    occs = _expand_ics(
        text,
        datetime(2026, 1, 14, tzinfo=_tz.utc),
        datetime(2026, 1, 16, tzinfo=_tz.utc),
    )
    got = sorted(o.start.value.day for o in occs)
    assert got == [14, 15]


def test_expand_non_recurring():
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\n"
        "DTSTART:20260601T100000Z\r\nDTEND:20260601T110000Z\r\nSUMMARY:One\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    from datetime import timezone as _tz

    occs = _expand_ics(text, datetime(2026, 6, 1, tzinfo=_tz.utc), datetime(2026, 6, 2, tzinfo=_tz.utc))
    assert len(occs) == 1
    assert occs[0].recurrence_id is None
    out = _expand_ics(text, datetime(2026, 7, 1, tzinfo=_tz.utc), datetime(2026, 7, 2, tzinfo=_tz.utc))
    assert out == []


def test_expand_rdate():
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\n"
        "DTSTART:20260601T100000Z\r\nDTEND:20260601T110000Z\r\n"
        "RDATE:20260615T100000Z,20260620T100000Z\r\nSUMMARY:R\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    from datetime import timezone as _tz

    occs = _expand_ics(text, datetime(2026, 6, 1, tzinfo=_tz.utc), datetime(2026, 7, 1, tzinfo=_tz.utc))
    days = sorted(o.start.value.day for o in occs)
    assert days == [1, 15, 20]


def test_expand_cancelled_override_removed():
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20260601T100000Z\r\nDTEND:20260601T110000Z\r\n"
        "RRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:S\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:x\r\nRECURRENCE-ID:20260602T100000Z\r\n"
        "DTSTART:20260602T100000Z\r\nDTEND:20260602T110000Z\r\nSTATUS:CANCELLED\r\n"
        "SUMMARY:S\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    from datetime import timezone as _tz

    occs = _expand_ics(text, datetime(2026, 6, 1, tzinfo=_tz.utc), datetime(2026, 7, 1, tzinfo=_tz.utc))
    days = sorted(o.start.value.day for o in occs)
    assert days == [1, 3]


def test_expand_all_day_recurring():
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\n"
        "DTSTART;VALUE=DATE:20260601\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nSUMMARY:AllDay\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    from datetime import timezone as _tz

    occs = _expand_ics(text, datetime(2026, 6, 1, tzinfo=_tz.utc), datetime(2026, 7, 1, tzinfo=_tz.utc))
    assert [o.start.value for o in occs] == [date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)]
    assert all(o.start.is_date for o in occs)
    assert occs[0].end.value == date(2026, 6, 2)
