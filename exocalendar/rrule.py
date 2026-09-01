"""Full RFC 5545 recurrence expansion.

Model: iterate base periods (year/month/week/day/...) from DTSTART; within each
period build the candidate day-set as an intersection of BY*-derived masks
(defaults injected from DTSTART), cross with the time-set, apply BYSETPOS,
then filter by DTSTART/UNTIL/COUNT. This realizes the RFC's expand/limit
table the same way python-dateutil does; tests hold the two engines equal.

Recurrence arithmetic is wall-clock: aware datetimes keep their tzinfo and
day/time fields are manipulated naively, matching CalDAV expectations.
"""

from __future__ import annotations

import re
from calendar import isleap, monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import product
from typing import Iterator

from .ical import (
    Component,
    DTValue,
    TZResolver,
    event_span,
    parse_dt,
    parse_dt_values,
    parse_duration,
)


class RRuleError(ValueError):
    pass


class RRuleOverflow(RRuleError):
    """A rule produced no occurrences within the scan bound."""


_FREQS = ["YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY", "MINUTELY", "SECONDLY"]
_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_BYDAY_RE = re.compile(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)")

# empty-period scan bounds before declaring the rule degenerate: ~120 years
# per frequency (rare conjunctions like "Sep 22 falling on a Friday" gap for
# years' worth of DAILY periods)
_EMPTY_LIMITS = {"YEARLY": 120, "MONTHLY": 1500, "WEEKLY": 6500, "DAILY": 45_000}
_SUBDAY_STEP_LIMIT = 200_000


def _int_list(text: str, lo: int, hi: int, allow_neg: bool, part: str) -> list[int]:
    out = []
    for piece in text.split(","):
        try:
            v = int(piece)
        except ValueError:
            raise RRuleError(f"bad {part} value: {piece!r}") from None
        ok = (lo <= v <= hi) or (allow_neg and lo <= -v <= hi)
        if not ok or v == 0 and lo > 0:
            raise RRuleError(f"{part} value out of range: {v}")
        out.append(v)
    return out


@dataclass(frozen=True)
class RRule:
    freq: str
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    until_is_date: bool = False
    bysecond: tuple[int, ...] = ()
    byminute: tuple[int, ...] = ()
    byhour: tuple[int, ...] = ()
    byday: tuple[tuple[int, int], ...] = ()  # (ordinal or 0, weekday 0=MO)
    bymonthday: tuple[int, ...] = ()
    byyearday: tuple[int, ...] = ()
    byweekno: tuple[int, ...] = ()
    bymonth: tuple[int, ...] = ()
    bysetpos: tuple[int, ...] = ()
    wkst: int = 0  # 0 = MO

    @classmethod
    def parse(cls, text: str) -> "RRule":
        text = text.strip()
        if not text:
            raise RRuleError("empty RRULE")
        parts: dict[str, str] = {}
        for piece in text.split(";"):
            if not piece:
                continue
            if "=" not in piece:
                raise RRuleError(f"malformed RRULE part: {piece!r}")
            k, v = piece.split("=", 1)
            k = k.upper()
            if k in parts:
                raise RRuleError(f"duplicate RRULE part: {k}")
            parts[k] = v

        if "FREQ" not in parts:
            raise RRuleError("RRULE has no FREQ")
        freq = parts.pop("FREQ").upper()
        if freq not in _FREQS:
            raise RRuleError(f"unknown FREQ: {freq}")

        kw: dict = {"freq": freq}
        if "INTERVAL" in parts:
            try:
                kw["interval"] = int(parts.pop("INTERVAL"))
            except ValueError:
                raise RRuleError("bad INTERVAL") from None
            if kw["interval"] < 1:
                raise RRuleError("INTERVAL must be >= 1")
        if "COUNT" in parts:
            try:
                kw["count"] = int(parts.pop("COUNT"))
            except ValueError:
                raise RRuleError("bad COUNT") from None
            if kw["count"] < 1:
                raise RRuleError("COUNT must be >= 1")
        if "UNTIL" in parts:
            raw = parts.pop("UNTIL")
            if re.fullmatch(r"\d{8}", raw):
                kw["until"] = datetime.strptime(raw, "%Y%m%d")
                kw["until_is_date"] = True
            elif re.fullmatch(r"\d{8}T\d{6}Z?", raw):
                naive = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
                kw["until"] = (
                    naive.replace(tzinfo=timezone.utc) if raw.endswith("Z") else naive
                )
            else:
                raise RRuleError(f"bad UNTIL: {raw!r}")
        if kw.get("count") is not None and kw.get("until") is not None:
            raise RRuleError("COUNT and UNTIL are mutually exclusive")
        if "WKST" in parts:
            w = parts.pop("WKST").upper()
            if w not in _WEEKDAYS:
                raise RRuleError(f"bad WKST: {w!r}")
            kw["wkst"] = _WEEKDAYS.index(w)
        if "BYDAY" in parts:
            byday = []
            for piece in parts.pop("BYDAY").split(","):
                m = _BYDAY_RE.fullmatch(piece)
                if not m:
                    raise RRuleError(f"bad BYDAY value: {piece!r}")
                n = int(m.group(1)) if m.group(1) else 0
                if m.group(1) and (n == 0 or abs(n) > 53):
                    raise RRuleError(f"bad BYDAY ordinal: {piece!r}")
                if n and freq not in ("MONTHLY", "YEARLY"):
                    raise RRuleError("BYDAY ordinals only valid for MONTHLY/YEARLY")
                byday.append((n, _WEEKDAYS.index(m.group(2))))
            kw["byday"] = tuple(byday)
        ranges = {
            "BYSECOND": ("bysecond", 0, 60, False),
            "BYMINUTE": ("byminute", 0, 59, False),
            "BYHOUR": ("byhour", 0, 23, False),
            "BYMONTHDAY": ("bymonthday", 1, 31, True),
            "BYYEARDAY": ("byyearday", 1, 366, True),
            "BYWEEKNO": ("byweekno", 1, 53, True),
            "BYMONTH": ("bymonth", 1, 12, False),
            "BYSETPOS": ("bysetpos", 1, 366, True),
        }
        for part, (attr, lo, hi, neg) in ranges.items():
            if part in parts:
                kw[attr] = tuple(_int_list(parts.pop(part), lo, hi, neg, part))
        if parts:
            raise RRuleError(f"unknown RRULE parts: {', '.join(sorted(parts))}")
        if kw.get("byweekno") and freq != "YEARLY":
            raise RRuleError("BYWEEKNO only valid with FREQ=YEARLY")
        if kw.get("byyearday") and freq in ("DAILY", "WEEKLY", "MONTHLY"):
            raise RRuleError("BYYEARDAY not valid with DAILY/WEEKLY/MONTHLY")
        has_by = any(
            kw.get(a)
            for a in (
                "bysecond", "byminute", "byhour", "byday",
                "bymonthday", "byyearday", "byweekno", "bymonth",
            )
        )
        if kw.get("bysetpos") and not has_by:
            raise RRuleError("BYSETPOS requires another BY* part")
        return cls(**kw)

    # -- iteration ------------------------------------------------------------

    def iterate(self, dtstart: date | datetime) -> Iterator[date | datetime]:
        date_mode = isinstance(dtstart, date) and not isinstance(dtstart, datetime)
        if date_mode:
            dt0 = datetime(dtstart.year, dtstart.month, dtstart.day)
        else:
            dt0 = dtstart
        tzinfo = dt0.tzinfo
        base = dt0.replace(tzinfo=None)

        until = self.until
        if until is not None:
            if until.tzinfo is not None and tzinfo is None:
                until = until.replace(tzinfo=None)
            elif until.tzinfo is None and tzinfo is not None:
                until = until.replace(tzinfo=timezone.utc)

        yielded = 0
        for naive in self._iterate_naive(base):
            candidate = naive.replace(tzinfo=tzinfo) if tzinfo else naive
            if until is not None and candidate > until:
                return
            out: date | datetime = candidate.date() if date_mode else candidate
            yield out
            yielded += 1
            if self.count is not None and yielded >= self.count:
                return

    def _timeset(self, base: datetime) -> list[time]:
        hours = self.byhour or (base.hour,)
        minutes = self.byminute or (base.minute,)
        seconds = self.bysecond or (base.second,)
        return sorted(time(h, m, s) for h, m, s in product(hours, minutes, seconds))

    def _iterate_naive(self, base: datetime) -> Iterator[datetime]:
        if self.freq in ("HOURLY", "MINUTELY", "SECONDLY"):
            yield from self._iterate_subday(base)
            return

        byday = self.byday
        bymonthday = self.bymonthday
        bymonth = self.bymonth
        if not (self.byweekno or self.byyearday or bymonthday or byday):
            if self.freq == "YEARLY":
                if not bymonth:
                    bymonth = (base.month,)
                bymonthday = (base.day,)
            elif self.freq == "MONTHLY":
                bymonthday = (base.day,)
            elif self.freq == "WEEKLY":
                byday = ((0, base.weekday()),)

        timeset = self._timeset(base)
        empty = 0
        period = 0
        while True:
            days = self._dayset(base, period, byday, bymonthday, bymonth)
            found = False
            if days:
                slots = [
                    datetime.combine(d, t) for d in days for t in timeset
                ]
                if self.bysetpos:
                    picked = []
                    for pos in self.bysetpos:
                        idx = pos - 1 if pos > 0 else len(slots) + pos
                        if 0 <= idx < len(slots):
                            picked.append(slots[idx])
                    slots = sorted(set(picked))
                for slot in slots:
                    if slot < base:
                        continue
                    found = True
                    yield slot
            if found:
                empty = 0
            else:
                empty += 1
                if empty > _EMPTY_LIMITS[self.freq]:
                    raise RRuleOverflow(f"rule produces no occurrences: {self}")
            period += 1

    # day-set per period, sorted -- YEARLY / MONTHLY / WEEKLY / DAILY
    def _dayset(
        self,
        base: datetime,
        period: int,
        byday: tuple[tuple[int, int], ...],
        bymonthday: tuple[int, ...],
        bymonth: tuple[int, ...],
    ) -> list[date]:
        if self.freq == "YEARLY":
            year = base.year + period * self.interval
            if year > 9000:
                raise RRuleOverflow("rule iterated past year 9000")
            days = self._year_days(year, byday, bymonthday, bymonth)
        elif self.freq == "MONTHLY":
            month0 = base.year * 12 + (base.month - 1) + period * self.interval
            year, month = divmod(month0, 12)
            month += 1
            if year > 9000:
                raise RRuleOverflow("rule iterated past year 9000")
            if bymonth and month not in bymonth:
                return []
            days = self._month_days(year, month, byday, bymonthday)
        elif self.freq == "WEEKLY":
            week0 = base.date() - timedelta(days=(base.weekday() - self.wkst) % 7)
            start = week0 + timedelta(days=7 * self.interval * period)
            days = []
            for i in range(7):
                d = start + timedelta(days=i)
                if period == 0 and d < base.date():
                    # the first period is the PARTIAL week from DTSTART to the
                    # week's end: earlier days are not BYSETPOS candidates
                    # (matches dateutil / deployed-client behavior)
                    continue
                if bymonth and d.month not in bymonth:
                    continue
                if bymonthday and not _monthday_match(d, bymonthday):
                    continue
                if byday and d.weekday() not in {wd for _n, wd in byday}:
                    continue
                days.append(d)
        else:  # DAILY
            d = base.date() + timedelta(days=self.interval * period)
            if d.year > 9000:
                raise RRuleOverflow("rule iterated past year 9000")
            if bymonth and d.month not in bymonth:
                return []
            if bymonthday and not _monthday_match(d, bymonthday):
                return []
            if byday and d.weekday() not in {wd for _n, wd in byday}:
                return []
            days = [d]
        return days

    def _year_days(
        self,
        year: int,
        byday: tuple[tuple[int, int], ...],
        bymonthday: tuple[int, ...],
        bymonth: tuple[int, ...],
    ) -> list[date]:
        year_len = 366 if isleap(year) else 365
        jan1 = date(year, 1, 1)
        days = [jan1 + timedelta(days=i) for i in range(year_len)]
        if bymonth:
            days = [d for d in days if d.month in bymonth]
        if self.byweekno:
            wmask = self._weekno_mask(year)
            days = [d for d in days if d in wmask]
        if self.byyearday:
            wanted = {(yd if yd > 0 else year_len + yd + 1) for yd in self.byyearday}
            days = [d for d in days if d.timetuple().tm_yday in wanted]
        if bymonthday:
            days = [d for d in days if _monthday_match(d, bymonthday)]
        if byday:
            plain = {wd for n, wd in byday if n == 0}
            ordinal_days: set[date] = set()
            for n, wd in byday:
                if n == 0:
                    continue
                if bymonth:
                    for month in bymonth:
                        picked = _nth_weekday_of_month(year, month, wd, n)
                        if picked:
                            ordinal_days.add(picked)
                else:
                    picked = _nth_weekday_of_year(year, wd, n)
                    if picked:
                        ordinal_days.add(picked)
            days = [d for d in days if d.weekday() in plain or d in ordinal_days]
        return days

    def _month_days(
        self,
        year: int,
        month: int,
        byday: tuple[tuple[int, int], ...],
        bymonthday: tuple[int, ...],
    ) -> list[date]:
        ndays = monthrange(year, month)[1]
        days = [date(year, month, d) for d in range(1, ndays + 1)]
        if bymonthday:
            days = [d for d in days if _monthday_match(d, bymonthday)]
        if byday:
            plain = {wd for n, wd in byday if n == 0}
            ordinal_days = set()
            for n, wd in byday:
                if n == 0:
                    continue
                picked = _nth_weekday_of_month(year, month, wd, n)
                if picked:
                    ordinal_days.add(picked)
            days = [d for d in days if d.weekday() in plain or d in ordinal_days]
        return days

    def _weekno_mask(self, year: int) -> set[date]:
        """Dates within `year` that fall in the requested (WKST-based) weeks."""
        mask: set[date] = set()

        def week1_start(y: int) -> date:
            jan1 = date(y, 1, 1)
            back = (jan1.weekday() - self.wkst) % 7
            candidate = jan1 - timedelta(days=back)
            # week must hold >= 4 days of year y
            if (candidate + timedelta(days=6)).year == y and (
                7 - (jan1 - candidate).days
            ) >= 4 or (jan1 - candidate).days <= 3:
                return candidate
            return candidate + timedelta(days=7)

        def weeks_in(y: int) -> int:
            return ((week1_start(y + 1) - week1_start(y)).days) // 7

        nweeks = weeks_in(year)
        w1 = week1_start(year)
        for wn in self.byweekno:
            n = wn if wn > 0 else nweeks + wn + 1
            if not 1 <= n <= nweeks:
                continue
            start = w1 + timedelta(days=7 * (n - 1))
            for i in range(7):
                d = start + timedelta(days=i)
                if d.year == year:
                    mask.add(d)
        # edge: requested week 1 of next year may begin in this December
        if any((wn == 1) for wn in self.byweekno):
            start = week1_start(year + 1)
            for i in range(7):
                d = start + timedelta(days=i)
                if d.year == year:
                    mask.add(d)
        # edge: January days may belong to the last week of the previous year
        prev_weeks = weeks_in(year - 1)
        wanted_prev = {
            (wn if wn > 0 else prev_weeks + wn + 1) for wn in self.byweekno
        }
        if prev_weeks in wanted_prev:
            start = week1_start(year - 1) + timedelta(days=7 * (prev_weeks - 1))
            for i in range(7):
                d = start + timedelta(days=i)
                if d.year == year:
                    mask.add(d)
        return mask

    # -- sub-day frequencies --------------------------------------------------

    def _iterate_subday(self, base: datetime) -> Iterator[datetime]:
        step = {
            "HOURLY": timedelta(hours=self.interval),
            "MINUTELY": timedelta(minutes=self.interval),
            "SECONDLY": timedelta(seconds=self.interval),
        }[self.freq]
        pointer = base
        scans = 0
        plain_wd = {wd for _n, wd in self.byday}
        while True:
            scans += 1
            if scans > _SUBDAY_STEP_LIMIT:
                raise RRuleOverflow(f"rule produces no occurrences: {self}")
            d = pointer.date()
            date_ok = (
                (not self.bymonth or d.month in self.bymonth)
                and (not self.bymonthday or _monthday_match(d, self.bymonthday))
                and (not plain_wd or d.weekday() in plain_wd)
                and (
                    not self.byyearday
                    or d.timetuple().tm_yday
                    in {
                        (yd if yd > 0 else (366 if isleap(d.year) else 365) + yd + 1)
                        for yd in self.byyearday
                    }
                )
            )
            if not date_ok:
                # jump to the first step at or past next midnight
                next_day = datetime.combine(d + timedelta(days=1), time())
                skips = max(1, -((pointer - next_day) // step))
                pointer += step * skips
                if pointer.year > 9000:
                    raise RRuleOverflow("rule iterated past year 9000")
                continue
            slots: list[datetime]
            if self.freq == "HOURLY":
                if self.byhour and pointer.hour not in self.byhour:
                    pointer += step
                    continue
                minutes = self.byminute or (base.minute,)
                seconds = self.bysecond or (base.second,)
                slots = [
                    pointer.replace(minute=m, second=s)
                    for m in sorted(minutes)
                    for s in sorted(seconds)
                ]
            elif self.freq == "MINUTELY":
                if (self.byhour and pointer.hour not in self.byhour) or (
                    self.byminute and pointer.minute not in self.byminute
                ):
                    pointer += step
                    continue
                seconds = self.bysecond or (base.second,)
                slots = [pointer.replace(second=s) for s in sorted(seconds)]
            else:  # SECONDLY
                if (
                    (self.byhour and pointer.hour not in self.byhour)
                    or (self.byminute and pointer.minute not in self.byminute)
                    or (self.bysecond and pointer.second not in self.bysecond)
                ):
                    pointer += step
                    continue
                slots = [pointer]
            if self.bysetpos:
                picked = []
                for pos in self.bysetpos:
                    idx = pos - 1 if pos > 0 else len(slots) + pos
                    if 0 <= idx < len(slots):
                        picked.append(slots[idx])
                slots = sorted(set(picked))
            emitted = False
            for slot in slots:
                if slot >= base:
                    emitted = True
                    yield slot
            if emitted:
                scans = 0
            pointer += step


def _monthday_match(d: date, bymonthday: tuple[int, ...]) -> bool:
    ndays = monthrange(d.year, d.month)[1]
    for md in bymonthday:
        if md > 0 and d.day == md:
            return True
        if md < 0 and d.day == ndays + md + 1:
            return True
    return False


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date | None:
    ndays = monthrange(year, month)[1]
    matches = [
        date(year, month, d)
        for d in range(1, ndays + 1)
        if date(year, month, d).weekday() == weekday
    ]
    try:
        return matches[n - 1] if n > 0 else matches[n]
    except IndexError:
        return None


def _nth_weekday_of_year(year: int, weekday: int, n: int) -> date | None:
    year_len = 366 if isleap(year) else 365
    jan1 = date(year, 1, 1)
    matches = [
        jan1 + timedelta(days=i)
        for i in range(year_len)
        if (jan1 + timedelta(days=i)).weekday() == weekday
    ]
    try:
        return matches[n - 1] if n > 0 else matches[n]
    except IndexError:
        return None


# --- occurrence expansion over VEVENT sets -----------------------------------

@dataclass
class Occurrence:
    uid: str
    recurrence_id: DTValue | None
    start: DTValue
    end: DTValue
    component: Component


def _instant(dtv: DTValue) -> datetime:
    """Comparable UTC instant; floating/date values are read as UTC."""
    v = dtv.value
    if dtv.is_date:
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def _rid_key(dtv: DTValue) -> str:
    return _instant(dtv).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def intersects(start: DTValue, end: DTValue, range_start: datetime, range_end: datetime) -> bool:
    s, e = _instant(start), _instant(end)
    if s == e:  # zero-length events occupy their start instant
        return range_start <= s < range_end
    return e > range_start and s < range_end


def expand(
    components: list[Component],
    tz: TZResolver,
    range_start: datetime,
    range_end: datetime,
    limit: int = 10_000,
) -> list[Occurrence]:
    """Expand a UID's VEVENT set (master + overrides) into range occurrences."""
    master: Component | None = None
    overrides: list[tuple[DTValue, Component]] = []
    for comp in components:
        rid_line = comp.get("RECURRENCE-ID")
        if rid_line is None:
            master = comp
        else:
            overrides.append((parse_dt(rid_line, tz), comp))

    occs: list[Occurrence] = []
    override_keys = {_rid_key(rid) for rid, _c in overrides}

    if master is not None:
        uid_line = master.get("UID")
        uid = uid_line.value if uid_line else ""
        start, end = event_span(master, tz)
        duration = end.value - start.value

        exdates: set[str] = set()
        for ex in master.get_all("EXDATE"):
            for dtv in parse_dt_values(ex, tz):
                exdates.add(_rid_key(dtv))

        rrule_line = master.get("RRULE")
        rdate_lines = master.get_all("RDATE")

        if rrule_line is None and not rdate_lines:
            if intersects(start, end, range_start, range_end):
                occs.append(Occurrence(uid, None, start, end, master))
        else:
            starts: list[DTValue] = []
            explicit_ends: dict[str, DTValue] = {}
            if rrule_line is not None:
                rule = RRule.parse(rrule_line.value)
                scanned = 0
                for value in rule.iterate(start.value):
                    dtv = DTValue(value=value, is_date=start.is_date, tzid=start.tzid)
                    if _instant(dtv) >= range_end:
                        break
                    starts.append(dtv)
                    scanned += 1
                    if scanned >= limit:
                        break
            else:
                starts.append(start)
            for rd in rdate_lines:
                if rd.param("VALUE") == "PERIOD":
                    for piece in rd.value.split(","):
                        if "/" not in piece:
                            continue
                        s_raw, e_raw = piece.split("/", 1)
                        from .ical import parse_dt_single

                        s_dtv = parse_dt_single(s_raw, rd.param("TZID"), False, tz)
                        if e_raw.startswith(("P", "+", "-")):
                            e_val = s_dtv.value + parse_duration(e_raw)
                        else:
                            e_val = parse_dt_single(e_raw, rd.param("TZID"), False, tz).value
                        starts.append(s_dtv)
                        explicit_ends[_rid_key(s_dtv)] = DTValue(
                            value=e_val, is_date=False, tzid=s_dtv.tzid
                        )
                else:
                    starts.extend(parse_dt_values(rd, tz))
                if rrule_line is None and start not in starts:
                    starts.append(start)

            seen: set[str] = set()
            for dtv in sorted(starts, key=_instant):
                key = _rid_key(dtv)
                if key in seen or key in exdates or key in override_keys:
                    seen.add(key)
                    continue
                seen.add(key)
                occ_end = explicit_ends.get(key) or DTValue(
                    value=dtv.value + duration, is_date=dtv.is_date, tzid=dtv.tzid
                )
                if intersects(dtv, occ_end, range_start, range_end):
                    occs.append(Occurrence(uid, None, dtv, occ_end, master))

    for rid, comp in overrides:
        status = comp.get("STATUS")
        if status is not None and status.value.upper() == "CANCELLED":
            continue
        uid_line = comp.get("UID")
        uid = uid_line.value if uid_line else ""
        start, end = event_span(comp, tz)
        if intersects(start, end, range_start, range_end):
            occs.append(Occurrence(uid, rid, start, end, comp))

    occs.sort(key=lambda o: _instant(o.start))
    return occs
