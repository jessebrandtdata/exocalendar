"""JSON API, ICS feeds, and static file serving for the web UI.

The API is a thin client of the same Store the CalDAV layer uses; every edit
is expressed as an .ics resource rewrite, so DAV clients and the web UI can
never disagree about state. Recurring-event edits follow calendar convention:
scope "this" writes a RECURRENCE-ID override, "future" splits the series
(UNTIL on the old master, remaining COUNT carried to the new one), "all"
rewrites the master.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import re
import secrets
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .ical import (
    Component,
    ContentLine,
    DTValue,
    TZResolver,
    escape_text,
    parse_dt,
    unescape_text,
)
from .rrule import RRule, RRuleError, expand
from .store import BadResource, CalInfo, PreconditionFailed, Store

STATIC_DIR = Path(__file__).parent / "static"

# ColorBrewer Dark2 — rotated across newly created calendars
PALETTE = [
    "#1b9e77", "#d95f02", "#7570b3", "#e7298a",
    "#66a61e", "#e6ab02", "#a6761d", "#666666",
]

_OCC_LIMIT = 2000


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _json(status: int, payload) -> tuple[int, dict, bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    return status, {"Content-Type": "application/json; charset=utf-8"}, body


def _cal_json(cal: CalInfo) -> dict:
    return {
        "id": cal.id,
        "displayname": cal.displayname,
        "color": cal.color,
        "description": cal.description,
        "order": cal.order,
        "feed_token": cal.feed_token,
    }


class WebApi:
    def __init__(self, store: Store):
        self.store = store

    # -- routing --------------------------------------------------------------

    def handle_api(self, method: str, path: str, body: bytes):
        parsed = urlparse(path)
        parts = [p for p in parsed.path.split("/") if p]  # ["api", ...]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parts[1:2] == ["import"]:
            payload = {}  # body is a raw .ics file, not JSON
        else:
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                return _json(400, {"error": "invalid JSON body"})
        try:
            return self._route_api(method.upper(), parts[1:], query, payload, body)
        except ApiError as exc:
            return _json(exc.status, {"error": exc.message})
        except PreconditionFailed as exc:
            return _json(409, {"error": str(exc)})
        except BadResource as exc:
            return _json(400, {"error": str(exc)})
        except KeyError as exc:
            return _json(404, {"error": f"not found: {exc}"})
        except (ValueError, RRuleError) as exc:
            return _json(400, {"error": str(exc)})

    def _route_api(self, method, parts, query, payload, raw_body):
        match (method, parts):
            case ("GET", ["calendars"]):
                return _json(200, [_cal_json(c) for c in self.store.list_calendars()])
            case ("POST", ["calendars"]):
                return self._create_calendar(payload)
            case ("PATCH", ["calendars", cal_id]):
                allowed = {
                    k: payload[k]
                    for k in ("displayname", "color", "description", "order")
                    if k in payload
                }
                info = self.store.update_calendar_props(cal_id, **allowed)
                return _json(200, _cal_json(info))
            case ("DELETE", ["calendars", cal_id]):
                self.store.delete_calendar(cal_id)
                return 204, {}, b""
            case ("POST", ["calendars", cal_id, "rotate-feed-token"]):
                info = self.store.update_calendar_props(
                    cal_id, feed_token=secrets.token_urlsafe(16)
                )
                return _json(200, _cal_json(info))
            case ("GET", ["occurrences"]):
                return self._occurrences(query)
            case ("POST", ["events"]):
                return self._create_event(payload)
            case ("PUT", ["events", cal_id, href]):
                return self._edit_event(cal_id, href, payload)
            case ("DELETE", ["events", cal_id, href]):
                return self._delete_event(cal_id, href, payload)
            case ("POST", ["import"]):
                cal_id = query.get("calendar", "")
                return self._import(cal_id, raw_body)
            case ("GET", ["export", name]) if name.endswith(".ics"):
                text = self._export(name[:-4])
                return 200, {"Content-Type": "text/calendar; charset=utf-8"}, text.encode()
        return _json(404, {"error": "not found"})

    # -- calendars ------------------------------------------------------------

    def _create_calendar(self, payload):
        displayname = str(payload.get("displayname") or "Calendar").strip() or "Calendar"
        cal_id = payload.get("id") or _slug(displayname)
        existing = {c.id for c in self.store.list_calendars()}
        base = cal_id
        n = 2
        while cal_id in existing:
            cal_id = f"{base}-{n}"
            n += 1
        color = payload.get("color") or PALETTE[len(existing) % len(PALETTE)]
        info = self.store.create_calendar(cal_id, displayname, color)
        return _json(201, _cal_json(info))

    # -- occurrences ----------------------------------------------------------

    def _occurrences(self, query):
        try:
            range_start = _instant(datetime.fromisoformat(query["start"]))
            range_end = _instant(datetime.fromisoformat(query["end"]))
        except (KeyError, ValueError):
            raise ApiError(400, "start and end must be ISO 8601 datetimes") from None
        wanted = query.get("calendars")
        cals = self.store.list_calendars()
        if wanted is not None:
            keep = set(wanted.split(","))
            cals = [c for c in cals if c.id in keep]
        out = []
        for cal in cals:
            for res in self.store.list_resources(cal.id):
                try:
                    vcal = Component.parse(res.ics_text)
                    tz = TZResolver.from_calendar(vcal)
                    occs = expand(
                        vcal.find_children("VEVENT"), tz,
                        range_start, range_end, limit=_OCC_LIMIT,
                    )
                except Exception as exc:  # noqa: BLE001
                    # never let one broken resource hide the whole calendar,
                    # but leave a trace so the omission is discoverable
                    import sys

                    print(
                        f"exocalendar: skipping unexpandable resource "
                        f"{cal.id}/{res.href}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                for occ in occs:
                    out.append(self._occ_json(cal, res, occ))
        out.sort(key=lambda o: (o["start"], o["cal"]))
        return _json(200, out)

    def _occ_json(self, cal: CalInfo, res, occ) -> dict:
        comp = occ.component
        rrule_line = comp.get("RRULE")
        is_recurring = (
            rrule_line is not None
            or comp.get("RDATE") is not None
            or occ.recurrence_id is not None
        )
        rid = occ.recurrence_id
        if rid is None and is_recurring:
            rid = occ.start
        return {
            "cal": cal.id,
            "color": cal.color,
            "href": res.href,
            "uid": occ.uid,
            "etag": res.etag,
            "recurrence_id": _iso(rid) if rid is not None else None,
            "start": _iso(occ.start),
            "end": _iso(occ.end),
            "all_day": occ.start.is_date,
            "summary": _text_of(comp, "SUMMARY"),
            "location": _text_of(comp, "LOCATION"),
            "description": _text_of(comp, "DESCRIPTION"),
            "rrule": rrule_line.value if rrule_line is not None else None,
            "is_recurring": is_recurring,
        }

    # -- event creation -------------------------------------------------------

    def _create_event(self, payload):
        cal_id = payload.get("cal") or ""
        if self.store.get_calendar(cal_id) is None:
            raise ApiError(404, f"no such calendar: {cal_id}")
        uid = str(uuid.uuid4())
        vevent = Component(name="VEVENT")
        vevent.set("UID", uid)
        vevent.set("DTSTAMP", _utcnow_basic())
        _apply_event_fields(vevent, payload)
        vcal = _wrap([vevent])
        href = _href_for(uid)
        res = self.store.put(cal_id, href, vcal.serialize())
        return _json(201, {"href": res.href, "etag": res.etag, "uid": uid})

    # -- event edits ----------------------------------------------------------

    def _load(self, cal_id, href, expected_etag):
        res = self.store.get(cal_id, href)
        if res is None:
            raise ApiError(404, f"no such event: {href}")
        if expected_etag and expected_etag != res.etag:
            raise PreconditionFailed("event was modified elsewhere; reload")
        vcal = Component.parse(res.ics_text)
        events = vcal.find_children("VEVENT")
        master = next((e for e in events if e.get("RECURRENCE-ID") is None), None)
        if master is None:
            raise ApiError(400, "resource has no master event")
        return res, vcal, master

    def _edit_event(self, cal_id, href, payload):
        scope = payload.get("scope", "all")
        res, vcal, master = self._load(cal_id, href, payload.get("etag"))
        if scope == "all":
            _apply_event_fields(master, payload)
        elif scope == "this":
            self._edit_this(vcal, master, payload)
        elif scope == "future":
            return self._edit_future(cal_id, href, res, vcal, master, payload)
        else:
            raise ApiError(400, f"unknown scope: {scope}")
        out = self.store.put(cal_id, href, vcal.serialize(), if_match=res.etag)
        return _json(200, {"href": out.href, "etag": out.etag})

    def _edit_this(self, vcal, master, payload):
        rid = _require_rid(payload)
        rid_line = _dt_line("RECURRENCE-ID", rid, master)
        rid_key = rid_line.serialize()
        # replace an existing override for the same occurrence
        vcal.children = [
            c
            for c in vcal.children
            if not (
                c.name == "VEVENT"
                and c.get("RECURRENCE-ID") is not None
                and c.get("RECURRENCE-ID").serialize() == rid_key
            )
        ]
        override = Component(name="VEVENT")
        override.set("UID", master.get("UID").value)
        override.set("DTSTAMP", _utcnow_basic())
        override.lines.append(rid_line)
        _apply_event_fields(override, payload, allow_rrule=False)
        vcal.children.append(override)

    def _edit_future(self, cal_id, href, res, vcal, master, payload):
        rid = _require_rid(payload)
        rule_text = _truncate_series(vcal, master, rid)
        self.store.put(cal_id, href, vcal.serialize(), if_match=res.etag)
        # new series from the cut point, carrying the remaining schedule
        new_payload = dict(payload)
        if "rrule" not in new_payload:
            new_payload["rrule"] = rule_text
        new_payload["cal"] = cal_id
        _status, headers, body = self._create_event(new_payload)
        return 200, headers, body  # an edit, even one that splits the series

    def _delete_event(self, cal_id, href, payload):
        scope = payload.get("scope", "all")
        res, vcal, master = self._load(cal_id, href, payload.get("etag"))
        if scope == "all":
            self.store.delete(cal_id, href, if_match=res.etag)
            return 204, {}, b""
        rid = _require_rid(payload)
        if scope == "this":
            rid_line = _dt_line("RECURRENCE-ID", rid, master)
            rid_key = rid_line.serialize()
            vcal.children = [
                c
                for c in vcal.children
                if not (
                    c.name == "VEVENT"
                    and c.get("RECURRENCE-ID") is not None
                    and c.get("RECURRENCE-ID").serialize() == rid_key
                )
            ]
            ex = _dt_line("EXDATE", rid, master)
            master.lines.append(ex)
        elif scope == "future":
            _truncate_series(vcal, master, rid)
        else:
            raise ApiError(400, f"unknown scope: {scope}")
        self.store.put(cal_id, href, vcal.serialize(), if_match=res.etag)
        return 204, {}, b""

    # -- import / export / feed -----------------------------------------------

    def _import(self, cal_id, raw_body):
        if self.store.get_calendar(cal_id) is None:
            raise ApiError(404, f"no such calendar: {cal_id}")
        from .ical import parse_all

        text = raw_body.decode("utf-8", errors="replace")
        try:
            cals = parse_all(text)
        except ValueError as exc:
            raise ApiError(400, f"unparseable ICS file: {exc}") from None
        timezones: list[Component] = []
        by_uid: dict[str, list[Component]] = {}
        for cal in cals:
            if cal.name != "VCALENDAR":
                continue
            timezones.extend(cal.find_children("VTIMEZONE"))
            for ev in cal.find_children("VEVENT"):
                uid_line = ev.get("UID")
                if uid_line is None or not uid_line.value:
                    continue
                by_uid.setdefault(uid_line.value, []).append(ev)
        imported = 0
        skipped = 0
        for uid, events in by_uid.items():
            resource = _wrap(events, timezones)
            try:
                self.store.put(cal_id, _href_for(uid), resource.serialize())
                imported += 1
            except (BadResource, ValueError):
                skipped += 1
        return _json(200, {"imported": imported, "skipped": skipped})

    def _export(self, cal_id: str) -> str:
        if self.store.get_calendar(cal_id) is None:
            raise ApiError(404, f"no such calendar: {cal_id}")
        events: list[Component] = []
        timezones: dict[str, Component] = {}
        for res in self.store.list_resources(cal_id):
            try:
                vcal = Component.parse(res.ics_text)
            except ValueError:
                continue
            for vtz in vcal.find_children("VTIMEZONE"):
                tzid = vtz.get("TZID")
                if tzid is not None:
                    timezones.setdefault(tzid.value, vtz)
            events.extend(vcal.find_children("VEVENT"))
        return _wrap(events, list(timezones.values())).serialize()

    def handle_feed(self, path: str):
        parsed = urlparse(path)
        m = re.fullmatch(r"/feed/([A-Za-z0-9_-]+)\.ics", parsed.path)
        token = {k: v[0] for k, v in parse_qs(parsed.query).items()}.get("t", "")
        if m:
            cal = self.store.get_calendar(m.group(1))
            if (
                cal is not None
                and token
                and hmac.compare_digest(token, cal.feed_token)
            ):
                text = self._export(cal.id)
                return 200, {"Content-Type": "text/calendar; charset=utf-8"}, text.encode()
        return 404, {"Content-Type": "text/plain"}, b"not found"

    # -- static ---------------------------------------------------------------

    def handle_static(self, method: str, path: str):
        if method.upper() not in ("GET", "HEAD"):
            return 405, {"Allow": "GET, HEAD"}, b""
        name = urlparse(path).path.lstrip("/") or "index.html"
        file = STATIC_DIR / name
        # resist traversal: only direct children of static/
        if "/" in name or not file.is_file():
            return 404, {"Content-Type": "text/plain"}, b"not found"
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        data = file.read_bytes()
        return 200, {"Content-Type": f"{ctype}; charset=utf-8"}, data


# --- event field helpers -----------------------------------------------------

def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40]
    return slug or "cal"


def _href_for(uid: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._@%-]", "_", uid)[:200].lstrip(".")
    return (safe or "event") + ".ics"


def _utcnow_basic() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _instant(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dtv: DTValue) -> str:
    return dtv.value.isoformat()


def _text_of(comp: Component, name: str) -> str:
    line = comp.get(name)
    return unescape_text(line.value) if line is not None else ""


def _parse_when(payload) -> tuple[DTValue, DTValue]:
    all_day = bool(payload.get("all_day"))
    try:
        if all_day:
            start = DTValue(date.fromisoformat(payload["start"]), True, None)
            end = DTValue(date.fromisoformat(payload["end"]), True, None)
        else:
            tzid = payload.get("tzid")
            s = datetime.fromisoformat(payload["start"])
            e = datetime.fromisoformat(payload["end"])
            if tzid:
                zone = ZoneInfo(tzid)
                s = _instant(s).astimezone(zone)
                e = _instant(e).astimezone(zone)
                start = DTValue(s, False, tzid)
                end = DTValue(e, False, tzid)
            else:
                start = DTValue(_instant(s).astimezone(timezone.utc), False, None)
                end = DTValue(_instant(e).astimezone(timezone.utc), False, None)
    except (KeyError, ValueError) as exc:
        raise ApiError(400, f"bad start/end: {exc}") from None
    if (end.value < start.value) if not all_day else (end.value <= start.value):
        raise ApiError(400, "end must be after start")
    return start, end


def _dtv_params(dtv: DTValue) -> tuple[str, list]:
    from .ical import serialize_dt

    return serialize_dt(dtv)


def _apply_event_fields(vevent: Component, payload, allow_rrule: bool = True):
    start, end = _parse_when(payload)
    sval, sparams = _dtv_params(start)
    eval_, eparams = _dtv_params(end)
    vevent.set("DTSTART", sval, sparams)
    vevent.set("DTEND", eval_, eparams)
    vevent.remove("DURATION")
    vevent.set("SUMMARY", escape_text(str(payload.get("summary", "")).strip() or "(untitled)"))
    for field, name in (("location", "LOCATION"), ("description", "DESCRIPTION")):
        value = str(payload.get(field, "") or "")
        if value:
            vevent.set(name, escape_text(value))
        else:
            vevent.remove(name)
    if allow_rrule and "rrule" in payload:
        rule_text = payload["rrule"]
        if rule_text:
            RRule.parse(rule_text)  # validate before storing
            vevent.set("RRULE", rule_text)
        else:
            vevent.remove("RRULE")


def _require_rid(payload) -> DTValue:
    raw = payload.get("recurrence_id")
    if not raw:
        raise ApiError(400, "recurrence_id is required for this scope")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return DTValue(date.fromisoformat(raw), True, None)
        return DTValue(datetime.fromisoformat(raw), False, None)
    except ValueError as exc:
        raise ApiError(400, f"bad recurrence_id: {exc}") from None


def _dt_line(name: str, rid: DTValue, master: Component) -> ContentLine:
    """Build a date/-time line matching the master DTSTART's form."""
    master_start = master.get("DTSTART")
    tzid = master_start.param("TZID") if master_start is not None else None
    is_date = rid.is_date or (
        master_start is not None and master_start.param("VALUE") == "DATE"
    )
    if is_date:
        value = rid.value if isinstance(rid.value, date) and not isinstance(rid.value, datetime) else rid.value.date()
        return ContentLine(name=name, params=[("VALUE", ["DATE"])], value=value.strftime("%Y%m%d"))
    dt = rid.value
    if tzid:
        dt = _instant(dt).astimezone(ZoneInfo(tzid))
        return ContentLine(
            name=name, params=[("TZID", [tzid])], value=dt.strftime("%Y%m%dT%H%M%S")
        )
    dt = _instant(dt).astimezone(timezone.utc)
    return ContentLine(name=name, params=[], value=dt.strftime("%Y%m%dT%H%M%SZ"))


def _truncate_series(vcal: Component, master: Component, rid: DTValue) -> str:
    """End the master's series just before `rid`; return the RRULE text the
    detached remainder should carry (remaining COUNT computed, else UNTIL kept)."""
    rrule_line = master.get("RRULE")
    if rrule_line is None:
        raise ApiError(400, "event is not recurring")
    rule = RRule.parse(rrule_line.value)
    tz = TZResolver.from_calendar(vcal)
    dtstart = parse_dt(master.get("DTSTART"), tz)

    if rid.is_date:
        cutoff_value = rid.value
        until_text = (cutoff_value - timedelta(days=1)).strftime("%Y%m%d")
        cutoff_cmp = datetime.combine(cutoff_value, time())
    else:
        cutoff_utc = _instant(rid.value).astimezone(timezone.utc)
        until_text = (cutoff_utc - timedelta(seconds=1)).strftime("%Y%m%dT%H%M%SZ")
        cutoff_cmp = cutoff_utc

    # count how many occurrences the old series keeps
    used = 0
    for value in rule.iterate(dtstart.value):
        inst = value if isinstance(value, datetime) else datetime.combine(value, time())
        if inst.tzinfo is not None:
            cmp = inst.astimezone(timezone.utc)
            cut = cutoff_cmp if cutoff_cmp.tzinfo else cutoff_cmp.replace(tzinfo=timezone.utc)
        else:
            cmp = inst
            cut = cutoff_cmp.replace(tzinfo=None) if cutoff_cmp.tzinfo else cutoff_cmp
        if cmp >= cut:
            break
        used += 1
        if used > 100_000:
            break

    parts = [p for p in rrule_line.value.split(";") if p and not p.upper().startswith(("UNTIL=", "COUNT="))]
    master.set("RRULE", ";".join(parts + [f"UNTIL={until_text}"]))

    remainder = parts[:]
    if rule.count is not None:
        remaining = max(rule.count - used, 1)
        remainder.append(f"COUNT={remaining}")
    elif rule.until is not None:
        for p in rrule_line.value.split(";"):
            if p.upper().startswith("UNTIL="):
                remainder.append(p)
    remainder_text = ";".join(remainder)

    # overrides and EXDATEs at/after the cut belong to the removed tail
    rid_cut = _dt_line("RECURRENCE-ID", rid, master)
    cut_key = rid_cut.value
    keep_children = []
    for child in vcal.children:
        if child.name == "VEVENT" and child.get("RECURRENCE-ID") is not None:
            if child.get("RECURRENCE-ID").value >= cut_key:
                continue
        keep_children.append(child)
    vcal.children = keep_children
    return remainder_text


def _wrap(events: list[Component], timezones: list[Component] | None = None) -> Component:
    vcal = Component(name="VCALENDAR")
    vcal.set("VERSION", "2.0")
    vcal.set("PRODID", "-//exocalendar//EN")
    for vtz in timezones or []:
        vcal.children.append(vtz)
    for ev in events:
        vcal.children.append(ev)
    return vcal
