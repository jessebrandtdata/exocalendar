"""RFC 5545 iCalendar parsing and serialization.

Losslessness contract: anything parsed serializes back byte-equivalent modulo
line folding and CRLF normalization — unknown properties, parameters, and
components are preserved verbatim and in order. Parameter values keep their
original quoting (stored raw, unquoted on access).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

FOLD_LIMIT = 75


class TZUnknown(ValueError):
    """A TZID could not be resolved from VTIMEZONEs or the host zoneinfo db."""


# --- folding -----------------------------------------------------------------

def unfold(text: str) -> list[str]:
    """Split into logical lines, joining folded continuations."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")):
            if lines:
                lines[-1] += raw[1:]
            elif raw[1:]:
                lines.append(raw[1:])
        elif raw:
            lines.append(raw)
    return lines


def fold_line(line: str) -> str:
    """Fold a logical line at FOLD_LIMIT octets, never splitting a UTF-8 sequence."""
    encoded = line.encode()
    if len(encoded) <= FOLD_LIMIT:
        return line
    parts: list[str] = []
    limit = FOLD_LIMIT
    rest = line
    while rest:
        chunk = rest
        while len(chunk.encode()) > limit:
            chunk = chunk[:-1]
        parts.append(chunk)
        rest = rest[len(chunk):]
        limit = FOLD_LIMIT - 1  # continuation lines lose one octet to the leading space
    return "\r\n ".join(parts)


# --- text escaping -----------------------------------------------------------

def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def unescape_text(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in "nN":
                out.append("\n")
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# --- content lines -----------------------------------------------------------

@dataclass
class ContentLine:
    """One logical line. `params` values are stored raw (quotes kept)."""

    name: str
    params: list[tuple[str, list[str]]] = field(default_factory=list)
    value: str = ""

    @classmethod
    def parse(cls, line: str) -> "ContentLine":
        name, params, rest = _parse_name_params(line)
        return cls(name=name.upper(), params=params, value=rest)

    def param(self, name: str) -> str | None:
        name = name.upper()
        for pname, values in self.params:
            if pname.upper() == name and values:
                return _unquote(values[0])
        return None

    def param_values(self, name: str) -> list[str]:
        name = name.upper()
        for pname, values in self.params:
            if pname.upper() == name:
                return [_unquote(v) for v in values]
        return []

    def set_param(self, name: str, value: str) -> None:
        quoted = _quote_if_needed(value)
        for i, (pname, _values) in enumerate(self.params):
            if pname.upper() == name.upper():
                self.params[i] = (pname, [quoted])
                return
        self.params.append((name.upper(), [quoted]))

    def serialize(self) -> str:
        parts = [self.name]
        for pname, values in self.params:
            parts.append(";" + pname + "=" + ",".join(values))
        return "".join(parts) + ":" + self.value


def _parse_name_params(line: str) -> tuple[str, list[tuple[str, list[str]]], str]:
    params: list[tuple[str, list[str]]] = []
    i = 0
    n = len(line)
    # property name
    while i < n and line[i] not in ";:":
        i += 1
    if i == n:
        raise ValueError(f"content line has no ':': {line[:80]!r}")
    name = line[:i]
    if not name:
        raise ValueError(f"content line has empty name: {line[:80]!r}")
    # parameters
    while i < n and line[i] == ";":
        i += 1
        j = i
        while j < n and line[j] != "=":
            if line[j] in ";:":
                raise ValueError(f"malformed parameter in: {line[:80]!r}")
            j += 1
        if j == n:
            raise ValueError(f"parameter without '=' in: {line[:80]!r}")
        pname = line[i:j]
        i = j + 1
        values: list[str] = []
        while True:
            if i < n and line[i] == '"':
                j = line.find('"', i + 1)
                if j == -1:
                    raise ValueError(f"unterminated quote in: {line[:80]!r}")
                values.append(line[i : j + 1])
                i = j + 1
            else:
                j = i
                while j < n and line[j] not in ",;:":
                    j += 1
                values.append(line[i:j])
                i = j
            if i < n and line[i] == ",":
                i += 1
                continue
            break
        params.append((pname, values))
    if i == n or line[i] != ":":
        raise ValueError(f"content line has no ':': {line[:80]!r}")
    return name, params, line[i + 1 :]


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw


def _quote_if_needed(value: str) -> str:
    if any(c in value for c in ':;,"'):
        return '"' + value.replace('"', "") + '"'
    return value


# --- components --------------------------------------------------------------

@dataclass
class Component:
    name: str
    lines: list[ContentLine] = field(default_factory=list)
    children: list["Component"] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "Component":
        comps = parse_all(text)
        if len(comps) != 1:
            raise ValueError(f"expected one top-level component, got {len(comps)}")
        return comps[0]

    def get(self, name: str) -> ContentLine | None:
        name = name.upper()
        for line in self.lines:
            if line.name == name:
                return line
        return None

    def get_all(self, name: str) -> list[ContentLine]:
        name = name.upper()
        return [l for l in self.lines if l.name == name]

    def set(self, name: str, value: str, params: list[tuple[str, list[str]]] | None = None) -> ContentLine:
        existing = self.get(name)
        if existing is not None:
            existing.value = value
            if params is not None:
                existing.params = params
            return existing
        line = ContentLine(name=name.upper(), params=params or [], value=value)
        self.lines.append(line)
        return line

    def remove(self, name: str) -> None:
        name = name.upper()
        self.lines = [l for l in self.lines if l.name != name]

    def find_children(self, name: str) -> list["Component"]:
        name = name.upper()
        return [c for c in self.children if c.name == name]

    def serialize(self) -> str:
        return "".join(fold_line(l) + "\r\n" for l in self._logical_lines())

    def _logical_lines(self) -> list[str]:
        out = [f"BEGIN:{self.name}"]
        for line in self.lines:
            out.append(line.serialize())
        for child in self.children:
            out.extend(child._logical_lines())
        out.append(f"END:{self.name}")
        return out


def parse_all(text: str) -> list[Component]:
    """Parse text into a list of top-level components."""
    top: list[Component] = []
    stack: list[Component] = []
    for logical in unfold(text):
        cl = ContentLine.parse(logical)
        if cl.name == "BEGIN":
            comp = Component(name=cl.value.upper())
            if stack:
                stack[-1].children.append(comp)
            else:
                top.append(comp)
            stack.append(comp)
        elif cl.name == "END":
            if not stack:
                raise ValueError(f"END:{cl.value} without matching BEGIN")
            comp = stack.pop()
            if comp.name != cl.value.upper():
                raise ValueError(f"END:{cl.value} does not match BEGIN:{comp.name}")
        else:
            if not stack:
                raise ValueError(f"property outside any component: {logical[:80]!r}")
            stack[-1].lines.append(cl)
    if stack:
        raise ValueError(f"unterminated component: {stack[-1].name}")
    return top


# --- dates, times, timezones -------------------------------------------------

@dataclass(frozen=True)
class DTValue:
    """A parsed DATE or DATE-TIME property value."""

    value: date | datetime
    is_date: bool
    tzid: str | None


class _Observance:
    """One STANDARD/DAYLIGHT block of a VTIMEZONE."""

    def __init__(self, comp: Component):
        self.is_daylight = comp.name == "DAYLIGHT"
        self.offset_from = _parse_utc_offset(comp.get("TZOFFSETFROM").value)
        self.offset_to = _parse_utc_offset(comp.get("TZOFFSETTO").value)
        name = comp.get("TZNAME")
        self.tzname = name.value if name else None
        self.dtstart = _parse_basic_dt(comp.get("DTSTART").value)
        rrule_line = comp.get("RRULE")
        self.rrule = (
            _parse_tz_rrule(rrule_line.value, self.dtstart) if rrule_line else None
        )
        self.rdates = []
        for rd in comp.get_all("RDATE"):
            for v in rd.value.split(","):
                self.rdates.append(_parse_basic_dt(v))

    def transitions_for_year(self, year: int) -> list[datetime]:
        if self.rrule is None:
            out = [self.dtstart] + self.rdates
            return [t for t in out if t.year == year]
        if year < self.dtstart.year:
            return []
        month, byday, bymonthday, until = self.rrule
        if until is not None:
            # UNTIL is UTC; compare in local wall time of the pre-transition offset
            until_local = until.replace(tzinfo=None) + self.offset_from
        else:
            until_local = None
        if byday is not None:
            n, weekday = byday
            day = _nth_weekday(year, month, weekday, n)
            if day is None:
                return []
        else:
            day = bymonthday
        t = datetime(year, month, day, self.dtstart.hour, self.dtstart.minute, self.dtstart.second)
        if until_local is not None and t > until_local:
            return []
        return [t]

    def latest_transition(self, upto: datetime, in_utc: bool) -> datetime | None:
        """Latest transition at-or-before `upto`, unbounded lookback.

        `in_utc=False`: compare in local wall time (returns wall time).
        `in_utc=True`: compare the transition's UTC instant (`t - offset_from`)
        against a naive-UTC `upto` (returns that UTC instant).
        """
        if self.rrule is None:
            best = None
            for t in [self.dtstart] + self.rdates:
                cmp = (t - self.offset_from) if in_utc else t
                if cmp <= upto and (best is None or cmp > best):
                    best = cmp
            return best
        _month, _byday, _bymonthday, until = self.rrule
        year = upto.year + 1
        if until is not None:
            until_local = until.replace(tzinfo=None) + self.offset_from
            year = min(year, until_local.year)
        floor = max(self.dtstart.year, year - 500)  # rule-less gaps are bounded
        while year >= floor:
            for t in reversed(self.transitions_for_year(year)):
                cmp = (t - self.offset_from) if in_utc else t
                if cmp <= upto:
                    return cmp
            year -= 1
        return None


_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> int | None:
    """Day-of-month of the nth <weekday> (n<0 counts from the end)."""
    from calendar import monthrange

    days_in_month = monthrange(year, month)[1]
    matches = [
        d for d in range(1, days_in_month + 1)
        if date(year, month, d).weekday() == weekday
    ]
    try:
        return matches[n - 1] if n > 0 else matches[n]
    except IndexError:
        return None


def _parse_tz_rrule(text: str, dtstart: datetime):
    """Parse the restricted yearly RRULE shapes VTIMEZONEs use."""
    parts = dict(p.split("=", 1) for p in text.split(";") if "=" in p)
    if parts.get("FREQ") != "YEARLY":
        raise TZUnknown(f"unsupported VTIMEZONE RRULE: {text}")
    month = int(parts["BYMONTH"].split(",")[0]) if "BYMONTH" in parts else dtstart.month
    byday = None
    bymonthday = None
    if "BYDAY" in parts:
        m = re.fullmatch(r"(-?\d+)?(MO|TU|WE|TH|FR|SA|SU)", parts["BYDAY"])
        if not m:
            raise TZUnknown(f"unsupported BYDAY in VTIMEZONE RRULE: {text}")
        n = int(m.group(1)) if m.group(1) else 1
        byday = (n, _WEEKDAYS.index(m.group(2)))
    elif "BYMONTHDAY" in parts:
        bymonthday = int(parts["BYMONTHDAY"].split(",")[0])
    else:
        bymonthday = 1
    until = None
    if "UNTIL" in parts:
        until = _parse_basic_dt(parts["UNTIL"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
    return (month, byday, bymonthday, until)


def _parse_utc_offset(text: str) -> timedelta:
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})(\d{2})?", text)
    if not m:
        raise ValueError(f"bad UTC offset: {text!r}")
    sign = -1 if m.group(1) == "-" else 1
    return sign * timedelta(
        hours=int(m.group(2)), minutes=int(m.group(3)), seconds=int(m.group(4) or 0)
    )


class _VTimezone(tzinfo):
    """tzinfo built from a VTIMEZONE component's observance rules."""

    def __init__(self, tzid: str, observances: list[_Observance]):
        self._tzid = tzid
        self._observances = observances

    def _active(self, upto: datetime, in_utc: bool) -> _Observance | None:
        best = self._active_with_time(upto, in_utc)
        return best[1] if best is not None else None

    def _active_with_time(
        self, upto: datetime, in_utc: bool
    ) -> tuple[datetime, "_Observance"] | None:
        """(latest transition ≤ upto, its observance), or None before history."""
        best: tuple[datetime, _Observance] | None = None
        for obs in self._observances:
            t = obs.latest_transition(upto, in_utc)
            if t is not None and (best is None or t > best[0]):
                best = (t, obs)
        return best

    def _next_transition(
        self, wall: datetime
    ) -> tuple[datetime, "_Observance"] | None:
        best: tuple[datetime, _Observance] | None = None
        for obs in self._observances:
            candidates = (
                [obs.dtstart] + obs.rdates
                if obs.rrule is None
                else [
                    t
                    for y in (wall.year, wall.year + 1)
                    for t in obs.transitions_for_year(y)
                ]
            )
            for t in candidates:
                if t > wall and (best is None or t < best[0]):
                    best = (t, obs)
        return best

    def _earliest(self) -> _Observance:
        return min(self._observances, key=lambda o: o.dtstart)

    def utcoffset(self, dt):
        if dt is None:
            return None
        wall = dt.replace(tzinfo=None)
        obs = self._active(wall, in_utc=False)
        # before all transitions the pre-transition (FROM) offset applies
        offset = obs.offset_to if obs is not None else self._earliest().offset_from
        if getattr(dt, "fold", 0):
            # second pass of a repeated (fall-back) hour: the next transition's
            # offset applies within the window it repeats
            nxt = self._next_transition(wall)
            if nxt is not None:
                t2, obs2 = nxt
                shrink = offset - obs2.offset_to
                if shrink > timedelta(0) and wall >= t2 - shrink:
                    return obs2.offset_to
        return offset

    def dst(self, dt):
        if dt is None:
            return None
        obs = self._active(dt.replace(tzinfo=None), in_utc=False)
        if obs is None:
            return timedelta(0)
        return (obs.offset_to - obs.offset_from) if obs.is_daylight else timedelta(0)

    def tzname(self, dt):
        if dt is None:
            return self._tzid
        obs = self._active(dt.replace(tzinfo=None), in_utc=False)
        if obs is None:
            return self._earliest().tzname or self._tzid
        return obs.tzname or self._tzid

    def fromutc(self, dt):
        # resolve in UTC: transition instants (t - offset_from) order totally,
        # so the repeated wall hour after a fall-back stays unambiguous
        naive = dt.replace(tzinfo=None)
        best = self._active_with_time(naive, in_utc=True)
        if best is None:
            off = self._earliest().offset_from
            return (naive + off).replace(tzinfo=self)
        t_utc, obs = best
        off = obs.offset_to
        # inside the repeated hour just after a fall-back this is the second
        # pass of the same wall time: mark it (PEP 495) so utcoffset() inverts
        shrink = obs.offset_from - off
        fold = 1 if shrink > timedelta(0) and naive - t_utc < shrink else 0
        return (naive + off).replace(tzinfo=self, fold=fold)

    def __repr__(self):
        return f"_VTimezone({self._tzid!r})"


class TZResolver:
    """Resolve TZIDs: a calendar's own VTIMEZONEs first, host zoneinfo second."""

    def __init__(self, zones: dict[str, tzinfo] | None = None):
        self._zones = dict(zones or {})
        self._cache: dict[str, tzinfo] = {}

    @classmethod
    def from_calendar(cls, cal: Component | None) -> "TZResolver":
        zones: dict[str, tzinfo] = {}
        if cal is not None:
            for vtz in cal.find_children("VTIMEZONE"):
                tzid_line = vtz.get("TZID")
                if tzid_line is None:
                    continue
                try:
                    observances = [
                        _Observance(c)
                        for c in vtz.children
                        if c.name in ("STANDARD", "DAYLIGHT")
                        and c.get("TZOFFSETFROM") is not None
                        and c.get("TZOFFSETTO") is not None
                        and c.get("DTSTART") is not None
                    ]
                except (ValueError, KeyError, TZUnknown):
                    # a zone we cannot model falls through to host zoneinfo
                    continue
                if observances:
                    zones[tzid_line.value] = _VTimezone(tzid_line.value, observances)
        return cls(zones)

    def resolve(self, tzid: str) -> tzinfo:
        if tzid in self._zones:
            return self._zones[tzid]
        if tzid in self._cache:
            return self._cache[tzid]
        try:
            zone = ZoneInfo(tzid)
        except Exception:
            # common alias form: "/freeassociation.sourceforge.net/America/New_York"
            try:
                zone = ZoneInfo(tzid.rsplit("/", 2)[-2] + "/" + tzid.rsplit("/", 2)[-1])
            except Exception:
                raise TZUnknown(f"cannot resolve TZID {tzid!r}") from None
        self._cache[tzid] = zone
        return zone


def _parse_basic_dt(value: str) -> datetime:
    """Parse RFC 5545 basic-format DATE-TIME (no tz application)."""
    m = re.fullmatch(r"(\d{8})T(\d{2})(\d{2})(\d{2})(Z?)", value)
    if not m:
        raise ValueError(f"bad DATE-TIME value: {value!r}")
    d = datetime.strptime(m.group(1), "%Y%m%d")
    result = d.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=int(m.group(4)))
    if m.group(5):
        result = result.replace(tzinfo=timezone.utc)
    return result


def parse_dt_single(value: str, params_tzid: str | None, is_date: bool, tz: TZResolver) -> DTValue:
    if is_date:
        if not re.fullmatch(r"\d{8}", value):
            raise ValueError(f"bad DATE value: {value!r}")
        return DTValue(value=datetime.strptime(value, "%Y%m%d").date(), is_date=True, tzid=None)
    dt = _parse_basic_dt(value)
    if dt.tzinfo is not None:  # ...Z form
        return DTValue(value=dt, is_date=False, tzid=None)
    if params_tzid:
        zone = tz.resolve(params_tzid)
        return DTValue(value=dt.replace(tzinfo=zone), is_date=False, tzid=params_tzid)
    return DTValue(value=dt, is_date=False, tzid=None)  # floating


def _prop_is_date(prop: ContentLine) -> bool:
    vtype = prop.param("VALUE")
    if vtype == "DATE":
        return True
    if vtype == "DATE-TIME":
        return False
    return "T" not in prop.value.split(",")[0]


def parse_dt(prop: ContentLine, tz: TZResolver) -> DTValue:
    """Parse a single-valued DATE/DATE-TIME property (DTSTART, DTEND, ...)."""
    return parse_dt_single(prop.value, prop.param("TZID"), _prop_is_date(prop), tz)


def parse_dt_values(prop: ContentLine, tz: TZResolver) -> list[DTValue]:
    """Parse a multi-valued DATE/DATE-TIME property (EXDATE, RDATE)."""
    is_date = _prop_is_date(prop)
    tzid = prop.param("TZID")
    return [
        parse_dt_single(v, tzid, is_date, tz) for v in prop.value.split(",") if v
    ]


def serialize_dt(dtv: DTValue) -> tuple[str, list[tuple[str, list[str]]]]:
    """Serialize a DTValue back to (value, params)."""
    if dtv.is_date:
        return dtv.value.strftime("%Y%m%d"), [("VALUE", ["DATE"])]
    dt = dtv.value
    if dtv.tzid:
        return dt.strftime("%Y%m%dT%H%M%S"), [("TZID", [_quote_if_needed(dtv.tzid)])]
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), []
    return dt.strftime("%Y%m%dT%H%M%S"), []


_DURATION_RE = re.compile(
    r"([+-]?)P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?"
)


def parse_duration(text: str) -> timedelta:
    m = _DURATION_RE.fullmatch(text)
    if not m or text.lstrip("+-") in ("P", "PT"):
        raise ValueError(f"bad duration: {text!r}")
    sign = -1 if m.group(1) == "-" else 1
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in m.groups()[1:])
    return sign * timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)


def serialize_duration(td: timedelta) -> str:
    sign = ""
    if td < timedelta(0):
        sign = "-"
        td = -td
    total = int(td.total_seconds())
    if total == 0:
        return "PT0S"
    if total % (7 * 86400) == 0:
        return f"{sign}P{total // (7 * 86400)}W"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    out = f"{sign}P"
    if days:
        out += f"{days}D"
    if hours or minutes or seconds:
        out += "T"
        if hours:
            out += f"{hours}H"
        if minutes:
            out += f"{minutes}M"
        if seconds:
            out += f"{seconds}S"
    return out


def event_span(vevent: Component, tz: TZResolver) -> tuple[DTValue, DTValue]:
    """(start, end) of a VEVENT per RFC 5545 defaulting rules."""
    dtstart_line = vevent.get("DTSTART")
    if dtstart_line is None:
        raise ValueError("VEVENT has no DTSTART")
    start = parse_dt(dtstart_line, tz)
    dtend_line = vevent.get("DTEND")
    if dtend_line is not None:
        return start, parse_dt(dtend_line, tz)
    dur_line = vevent.get("DURATION")
    if dur_line is not None:
        dur = parse_duration(dur_line.value)
        return start, DTValue(value=start.value + dur, is_date=start.is_date, tzid=start.tzid)
    if start.is_date:
        return start, DTValue(value=start.value + timedelta(days=1), is_date=True, tzid=None)
    return start, start
