"""WebDAV/CalDAV protocol logic.

Pure request→response: `DavHandlerLogic.handle(method, path, headers, body)`
returns `(status, headers, body_bytes)` with no knowledge of sockets or auth,
so the whole protocol surface is unit-testable.

Layout: `/dav/` (root) → principal `/dav/u/` (also the calendar home) →
calendars `/dav/u/<cal>/` → resources `/dav/u/<cal>/<href>`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .ical import Component, TZResolver
from .rrule import expand
from .store import (
    BadResource,
    CalInfo,
    PreconditionFailed,
    Resource,
    StaleSyncToken,
    Store,
)

NS_D = "DAV:"
NS_C = "urn:ietf:params:xml:ns:caldav"
NS_CS = "http://calendarserver.org/ns/"
NS_A = "http://apple.com/ns/ical/"

for prefix, uri in (("d", NS_D), ("c", NS_C), ("cs", NS_CS), ("a", NS_A)):
    ET.register_namespace(prefix, uri)

D = "{%s}" % NS_D
C = "{%s}" % NS_C
CS = "{%s}" % NS_CS
A = "{%s}" % NS_A

_SYNC_PREFIX = "urn:exocalendar:sync:"

_ALLOW = "OPTIONS, PROPFIND, PROPPATCH, REPORT, MKCALENDAR, GET, HEAD, PUT, DELETE"

_FAR_PAST = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FAR_FUTURE = datetime(2200, 1, 1, tzinfo=timezone.utc)


def _xml(el: ET.Element) -> bytes:
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(el)


def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def _status_line(code: int) -> str:
    reasons = {200: "OK", 404: "Not Found", 403: "Forbidden", 507: "Insufficient Storage"}
    return f"HTTP/1.1 {code} {reasons.get(code, 'Status')}"


def _strip_etag(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    return raw.strip('"')


class DavHandlerLogic:
    def __init__(self, store: Store):
        self.store = store

    # -- entry point ----------------------------------------------------------

    def handle(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        path = path.split("?", 1)[0]
        if path == "/.well-known/caldav":
            return 301, {"Location": "/dav/"}, b""
        try:
            return self._dispatch(method.upper(), path, headers, body)
        except StaleSyncToken:
            err = ET.Element(f"{D}error")
            _sub(err, f"{D}valid-sync-token")
            return 403, {"Content-Type": "application/xml; charset=utf-8"}, _xml(err)
        except PreconditionFailed:
            return 412, {}, b""
        except BadResource as exc:
            return 400, {"Content-Type": "text/plain"}, str(exc).encode()
        except (KeyError, FileNotFoundError):
            return 404, {}, b""
        except ET.ParseError:
            return 400, {"Content-Type": "text/plain"}, b"malformed XML body"

    def _dispatch(self, method, path, headers, body):
        node = self._resolve(path)
        if method == "OPTIONS":
            return 200, {"DAV": "1, 3, calendar-access", "Allow": _ALLOW}, b""
        if node is None and method not in ("PUT", "MKCALENDAR", "MKCOL"):
            return 404, {}, b""
        if method == "PROPFIND":
            return self._propfind(node, headers, body)
        if method == "PROPPATCH":
            return self._proppatch(node, body)
        if method in ("MKCALENDAR", "MKCOL"):
            return self._mkcalendar(path, body)
        if method in ("GET", "HEAD"):
            return self._get(node, include_body=method == "GET")
        if method == "PUT":
            return self._put(path, headers, body)
        if method == "DELETE":
            return self._delete(node)
        if method == "REPORT":
            return self._report(node, body)
        return 405, {"Allow": _ALLOW}, b""

    # -- node model -----------------------------------------------------------

    def _resolve(self, path: str):
        """Return ('root'|'principal', None) | ('calendar', CalInfo) |
        ('resource', (CalInfo, Resource)) | None."""
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] != "dav":
            return None
        parts = parts[1:]
        if not parts:
            return ("root", None)
        if parts[0] != "u":
            return None
        parts = parts[1:]
        if not parts:
            return ("principal", None)
        cal = self.store.get_calendar(parts[0])
        if cal is None:
            return None
        parts = parts[1:]
        if not parts:
            return ("calendar", cal)
        if len(parts) > 1:
            return None
        res = self.store.get(cal.id, parts[0])
        if res is None:
            return None
        return ("resource", (cal, res))

    @staticmethod
    def _href(node) -> str:
        kind, data = node
        if kind == "root":
            return "/dav/"
        if kind == "principal":
            return "/dav/u/"
        if kind == "calendar":
            return f"/dav/u/{data.id}/"
        cal, res = data
        return f"/dav/u/{cal.id}/{res.href}"

    # -- PROPFIND -------------------------------------------------------------

    def _propfind(self, node, headers, body):
        depth = headers.get("Depth", "infinity").lower()
        wanted = self._parse_propfind(body)
        targets = [node]
        if depth != "0":
            kind, data = node
            if kind == "principal":
                targets += [("calendar", c) for c in self.store.list_calendars()]
            elif kind == "calendar":
                targets += [
                    ("resource", (data, r)) for r in self.store.list_resources(data.id)
                ]
        ms = ET.Element(f"{D}multistatus")
        for target in targets:
            self._prop_response(ms, target, wanted)
        return 207, {"Content-Type": "application/xml; charset=utf-8"}, _xml(ms)

    @staticmethod
    def _parse_propfind(body: bytes) -> list[str] | None:
        """Requested prop tags, or None for allprop."""
        if not body.strip():
            return None
        root = ET.fromstring(body)
        prop = root.find(f"{D}prop")
        if prop is None:  # allprop / propname
            return None
        return [child.tag for child in prop]

    _ALLPROP = [
        f"{D}resourcetype", f"{D}displayname", f"{D}getetag", f"{D}getcontenttype",
        f"{D}current-user-principal", f"{CS}getctag", f"{A}calendar-color",
        f"{D}sync-token",
    ]

    def _prop_response(self, ms: ET.Element, node, wanted: list[str] | None):
        resp = _sub(ms, f"{D}response")
        _sub(resp, f"{D}href", self._href(node))
        found: list[ET.Element] = []
        missing: list[str] = []
        for tag in wanted if wanted is not None else self._ALLPROP:
            el = self._prop_value(node, tag)
            if el is not None:
                found.append(el)
            elif wanted is not None:
                missing.append(tag)
        if found or not missing:
            ps = _sub(resp, f"{D}propstat")
            prop = _sub(ps, f"{D}prop")
            prop.extend(found)
            _sub(ps, f"{D}status", _status_line(200))
        if missing:
            ps = _sub(resp, f"{D}propstat")
            prop = _sub(ps, f"{D}prop")
            for tag in missing:
                _sub(prop, tag)
            _sub(ps, f"{D}status", _status_line(404))

    def _prop_value(self, node, tag: str) -> ET.Element | None:
        kind, data = node
        el = ET.Element(tag)
        if tag == f"{D}resourcetype":
            _sub(el, f"{D}collection") if kind != "resource" else None
            if kind == "principal":
                _sub(el, f"{D}principal")
            if kind == "calendar":
                _sub(el, f"{C}calendar")
            return el
        if tag == f"{D}current-user-principal" or tag == f"{D}principal-URL":
            _sub(el, f"{D}href", "/dav/u/")
            return el
        if tag == f"{D}owner":
            _sub(el, f"{D}href", "/dav/u/")
            return el
        if tag == f"{C}calendar-home-set" and kind == "principal":
            _sub(el, f"{D}href", "/dav/u/")
            return el
        if tag == f"{D}current-user-privilege-set":
            for priv in (f"{D}read", f"{D}write", f"{D}write-content", f"{D}all"):
                _sub(_sub(el, f"{D}privilege"), priv)
            return el
        if tag == f"{D}supported-report-set" and kind == "calendar":
            for rep in (f"{C}calendar-query", f"{C}calendar-multiget", f"{D}sync-collection"):
                _sub(_sub(_sub(el, f"{D}supported-report"), f"{D}report"), rep)
            return el
        if kind == "principal" and tag == f"{D}displayname":
            el.text = "exocalendar"
            return el
        if kind == "calendar":
            cal: CalInfo = data
            if tag == f"{D}displayname":
                el.text = cal.displayname
                return el
            if tag == f"{CS}getctag":
                el.text = cal.ctag
                return el
            if tag == f"{D}sync-token":
                seq, _c, _d = self.store.sync_delta(cal.id, None)
                el.text = _SYNC_PREFIX + seq
                return el
            if tag == f"{A}calendar-color":
                el.text = cal.color
                return el
            if tag == f"{C}supported-calendar-component-set":
                comp = _sub(el, f"{C}comp")
                comp.set("name", "VEVENT")
                return el
            if tag == f"{C}calendar-description":
                el.text = cal.description
                return el
        if kind == "resource":
            _cal, res = data
            if tag == f"{D}getetag":
                el.text = f'"{res.etag}"'
                return el
            if tag == f"{D}getcontenttype":
                el.text = "text/calendar; charset=utf-8"
                return el
            if tag == f"{D}getcontentlength":
                el.text = str(len(res.ics_text.encode()))
                return el
            if tag == f"{C}calendar-data":
                el.text = res.ics_text
                return el
        return None

    # -- PROPPATCH ------------------------------------------------------------

    _PATCHABLE = {}

    def _proppatch(self, node, body):
        kind, cal = node
        if kind != "calendar":
            return 403, {}, b""
        root = ET.fromstring(body)
        ok: list[str] = []
        refused: list[str] = []
        updates: dict[str, str] = {}
        for setel in root.findall(f"{D}set"):
            prop = setel.find(f"{D}prop")
            if prop is None:
                continue
            for child in prop:
                if child.tag == f"{D}displayname":
                    updates["displayname"] = child.text or ""
                    ok.append(child.tag)
                elif child.tag == f"{A}calendar-color":
                    updates["color"] = child.text or ""
                    ok.append(child.tag)
                elif child.tag == f"{C}calendar-description":
                    updates["description"] = child.text or ""
                    ok.append(child.tag)
                else:
                    refused.append(child.tag)
        if updates:
            self.store.update_calendar_props(cal.id, **updates)
        ms = ET.Element(f"{D}multistatus")
        resp = _sub(ms, f"{D}response")
        _sub(resp, f"{D}href", self._href(node))
        for tags, code in ((ok, 200), (refused, 403)):
            if tags:
                ps = _sub(resp, f"{D}propstat")
                prop = _sub(ps, f"{D}prop")
                for tag in tags:
                    _sub(prop, tag)
                _sub(ps, f"{D}status", _status_line(code))
        return 207, {"Content-Type": "application/xml; charset=utf-8"}, _xml(ms)

    # -- MKCALENDAR -----------------------------------------------------------

    def _mkcalendar(self, path, body):
        m = re.fullmatch(r"/dav/u/([^/]+)/?", path)
        if not m:
            return 403, {}, b""
        cal_id = m.group(1)
        if self.store.get_calendar(cal_id) is not None:
            return 405, {}, b""
        displayname, color = cal_id, "#1b9e77"
        if body.strip():
            root = ET.fromstring(body)
            for prop in root.iter(f"{D}prop"):
                dn = prop.find(f"{D}displayname")
                if dn is not None and dn.text:
                    displayname = dn.text
                col = prop.find(f"{A}calendar-color")
                if col is not None and col.text:
                    color = col.text
        try:
            self.store.create_calendar(cal_id, displayname, color)
        except ValueError:
            return 403, {"Content-Type": "text/plain"}, b"invalid calendar id"
        return 201, {}, b""

    # -- GET / PUT / DELETE ---------------------------------------------------

    def _get(self, node, include_body: bool):
        kind, data = node
        if kind != "resource":
            return 405, {"Allow": "OPTIONS, PROPFIND"}, b""
        _cal, res = data
        payload = res.ics_text.encode()
        headers = {
            "Content-Type": "text/calendar; charset=utf-8",
            "ETag": f'"{res.etag}"',
            "Content-Length": str(len(payload)),
        }
        return 200, headers, payload if include_body else b""

    def _put(self, path, headers, body):
        m = re.fullmatch(r"/dav/u/([^/]+)/([^/]+)", path)
        if not m:
            return 403, {}, b""
        cal_id, href = m.group(1), m.group(2)
        if_match = headers.get("If-Match")
        if_none_match = headers.get("If-None-Match", "").strip() == "*"
        existed = self.store.get(cal_id, href) is not None
        res = self.store.put(
            cal_id,
            href,
            body.decode("utf-8"),
            if_match=_strip_etag(if_match) if if_match else None,
            if_none_match=if_none_match,
        )
        return (204 if existed else 201), {"ETag": f'"{res.etag}"'}, b""

    def _delete(self, node):
        kind, data = node
        if kind == "calendar":
            self.store.delete_calendar(data.id)
            return 204, {}, b""
        if kind == "resource":
            cal, res = data
            self.store.delete(cal.id, res.href)
            return 204, {}, b""
        return 403, {}, b""

    # -- REPORT ---------------------------------------------------------------

    def _report(self, node, body):
        kind, cal = node
        if kind != "calendar":
            return 403, {}, b""
        root = ET.fromstring(body)
        if root.tag == f"{C}calendar-query":
            return self._report_query(cal, root)
        if root.tag == f"{C}calendar-multiget":
            return self._report_multiget(cal, root)
        if root.tag == f"{D}sync-collection":
            return self._report_sync(cal, root)
        return 403, {"Content-Type": "text/plain"}, b"unsupported report"

    @staticmethod
    def _wanted_props(root) -> list[str]:
        prop = root.find(f"{D}prop")
        return [child.tag for child in prop] if prop is not None else [f"{D}getetag"]

    def _report_query(self, cal: CalInfo, root):
        wanted = self._wanted_props(root)
        time_range = None
        comp_ok = True
        filt = root.find(f"{C}filter")
        if filt is not None:
            outer = filt.find(f"{C}comp-filter")
            if outer is not None:
                if (outer.get("name") or "").upper() != "VCALENDAR":
                    comp_ok = False
                inner = outer.find(f"{C}comp-filter")
                if inner is not None:
                    if (inner.get("name") or "").upper() != "VEVENT":
                        comp_ok = False
                    tr = inner.find(f"{C}time-range")
                    if tr is not None:
                        time_range = (
                            _parse_utc(tr.get("start"), _FAR_PAST),
                            _parse_utc(tr.get("end"), _FAR_FUTURE),
                        )
        ms = ET.Element(f"{D}multistatus")
        if comp_ok:
            for res in self.store.list_resources(cal.id):
                if time_range is not None and not self._resource_in_range(
                    res, time_range
                ):
                    continue
                self._prop_response(ms, ("resource", (cal, res)), wanted)
        return 207, {"Content-Type": "application/xml; charset=utf-8"}, _xml(ms)

    @staticmethod
    def _resource_in_range(res: Resource, time_range) -> bool:
        try:
            vcal = Component.parse(res.ics_text)
            tz = TZResolver.from_calendar(vcal)
            occs = expand(vcal.find_children("VEVENT"), tz, *time_range, limit=1000)
        except Exception:
            # a stored resource we cannot expand should still be visible
            return True
        return bool(occs)

    def _report_multiget(self, cal: CalInfo, root):
        wanted = self._wanted_props(root)
        ms = ET.Element(f"{D}multistatus")
        for href_el in root.findall(f"{D}href"):
            href = (href_el.text or "").strip()
            name = href.rstrip("/").rsplit("/", 1)[-1]
            res = self.store.get(cal.id, name) if name else None
            if res is None:
                resp = _sub(ms, f"{D}response")
                _sub(resp, f"{D}href", href)
                _sub(resp, f"{D}status", _status_line(404))
            else:
                self._prop_response(ms, ("resource", (cal, res)), wanted)
        return 207, {"Content-Type": "application/xml; charset=utf-8"}, _xml(ms)

    def _report_sync(self, cal: CalInfo, root):
        wanted = self._wanted_props(root)
        raw = (root.findtext(f"{D}sync-token") or "").strip()
        token = raw.removeprefix(_SYNC_PREFIX) if raw else None
        new_token, changed, deleted = self.store.sync_delta(cal.id, token)
        ms = ET.Element(f"{D}multistatus")
        for href in changed:
            res = self.store.get(cal.id, href)
            if res is not None:
                self._prop_response(ms, ("resource", (cal, res)), wanted)
        for href in deleted:
            resp = _sub(ms, f"{D}response")
            _sub(resp, f"{D}href", f"/dav/u/{cal.id}/{href}")
            _sub(resp, f"{D}status", _status_line(404))
        _sub(ms, f"{D}sync-token", _SYNC_PREFIX + new_token)
        return 207, {"Content-Type": "application/xml; charset=utf-8"}, _xml(ms)


def _parse_utc(raw: str | None, default: datetime) -> datetime:
    if not raw:
        return default
    m = re.fullmatch(r"(\d{8})T(\d{6})Z?", raw)
    if not m:
        return default
    return datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
