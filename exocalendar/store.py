"""Filesystem event storage.

Layout (human-inspectable, rsync-friendly):

    <data_dir>/calendars/<calendar-id>/
        .props.json     displayname, color, description, order, feed_token
        .journal.json   sync journal: monotonically increasing change log
        <href>          one .ics resource (a UID's VEVENT set) per file

ETag = sha256 of the resource bytes; ctag = sha256 over member (href, etag)
pairs; sync tokens are journal sequence numbers. Writes are atomic
(tmp + rename) and serialized per calendar with an in-process lock —
exocalendar is a single-process server.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from .ical import Component

_CAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_HREF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@%-]{0,253}")
_JOURNAL_KEEP = 5000


class PreconditionFailed(Exception):
    """An If-Match / If-None-Match condition did not hold."""


class StaleSyncToken(Exception):
    """The sync token is unknown or pruned; client must resync from scratch."""


class BadResource(ValueError):
    """The uploaded body is not a storable calendar resource."""


@dataclass(frozen=True)
class CalInfo:
    id: str
    displayname: str
    color: str
    description: str
    order: int
    ctag: str
    feed_token: str


@dataclass(frozen=True)
class Resource:
    href: str
    etag: str
    ics_text: str
    uid: str


def _check_cal_id(cal_id: str) -> str:
    if not _CAL_ID_RE.fullmatch(cal_id or ""):
        raise ValueError(f"invalid calendar id: {cal_id!r}")
    return cal_id


def _check_href(href: str) -> str:
    if not _HREF_RE.fullmatch(href or ""):
        raise ValueError(f"invalid resource name: {href!r}")
    return href


def _etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.calendars_dir = self.data_dir / "calendars"
        self.calendars_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, cal_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(cal_id, threading.Lock())

    def _cal_dir(self, cal_id: str, must_exist: bool = True) -> Path:
        path = self.calendars_dir / _check_cal_id(cal_id)
        if must_exist and not (path / ".props.json").is_file():
            raise KeyError(f"no such calendar: {cal_id}")
        return path

    # -- calendars ------------------------------------------------------------

    def create_calendar(
        self,
        cal_id: str,
        displayname: str,
        color: str,
        description: str = "",
        order: int = 0,
    ) -> CalInfo:
        path = self._cal_dir(cal_id, must_exist=False)
        with self._lock(cal_id):
            if (path / ".props.json").exists():
                raise ValueError(f"calendar exists: {cal_id}")
            path.mkdir(parents=True, exist_ok=True)
            props = {
                "displayname": displayname,
                "color": color,
                "description": description,
                "order": order,
                "feed_token": secrets.token_urlsafe(16),
            }
            _write_json(path / ".props.json", props)
            _write_json(path / ".journal.json", {"seq": 0, "floor": 0, "entries": []})
        return self.get_calendar(cal_id)

    def get_calendar(self, cal_id: str) -> CalInfo | None:
        try:
            path = self._cal_dir(cal_id)
        except (KeyError, ValueError):
            return None
        props = _read_json(path / ".props.json")
        return CalInfo(
            id=cal_id,
            displayname=props.get("displayname", cal_id),
            color=props.get("color", ""),
            description=props.get("description", ""),
            order=int(props.get("order", 0)),
            ctag=self._ctag(path),
            feed_token=props.get("feed_token", ""),
        )

    def list_calendars(self) -> list[CalInfo]:
        out = []
        for child in sorted(self.calendars_dir.iterdir()):
            if child.is_dir() and (child / ".props.json").is_file():
                info = self.get_calendar(child.name)
                if info is not None:
                    out.append(info)
        out.sort(key=lambda c: (c.order, c.id))
        return out

    def update_calendar_props(self, cal_id: str, **updates) -> CalInfo:
        allowed = {"displayname", "color", "description", "order", "feed_token"}
        bad = set(updates) - allowed
        if bad:
            raise ValueError(f"unknown calendar props: {sorted(bad)}")
        path = self._cal_dir(cal_id)
        with self._lock(cal_id):
            props = _read_json(path / ".props.json")
            props.update(updates)
            _write_json(path / ".props.json", props)
        return self.get_calendar(cal_id)

    def delete_calendar(self, cal_id: str) -> None:
        path = self._cal_dir(cal_id)
        with self._lock(cal_id):
            shutil.rmtree(path)

    def _ctag(self, path: Path) -> str:
        h = hashlib.sha256()
        for f in sorted(path.glob("*.ics")):
            h.update(f.name.encode())
            h.update(_etag(f.read_bytes()).encode())
        return h.hexdigest()

    # -- resources ------------------------------------------------------------

    def list_resources(self, cal_id: str) -> list[Resource]:
        path = self._cal_dir(cal_id)
        out = []
        for f in sorted(path.glob("*.ics")):
            res = self.get(cal_id, f.name)
            if res is not None:
                out.append(res)
        return out

    def get(self, cal_id: str, href: str) -> Resource | None:
        path = self._cal_dir(cal_id)
        _check_href(href)
        f = path / href
        if not f.is_file():
            return None
        data = f.read_bytes()
        text = data.decode("utf-8", errors="replace")
        return Resource(href=href, etag=_etag(data), ics_text=text, uid=_uid_of(text))

    def put(
        self,
        cal_id: str,
        href: str,
        ics_text: str,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> Resource:
        path = self._cal_dir(cal_id)
        _check_href(href)
        uid = _validate_resource(ics_text)
        data = ics_text.encode()
        with self._lock(cal_id):
            f = path / href
            existing = f.read_bytes() if f.is_file() else None
            if if_none_match and existing is not None:
                raise PreconditionFailed(f"{href} already exists")
            if if_match is not None and (
                existing is None or _etag(existing) != if_match
            ):
                raise PreconditionFailed(f"etag mismatch on {href}")
            if existing is not None:
                old_uid = _uid_of(existing.decode("utf-8", errors="replace"))
                if old_uid and old_uid != uid:
                    raise BadResource(
                        f"resource UID may not change ({old_uid!r} -> {uid!r})"
                    )
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(f)
            self._journal_append(path, href, "changed")
        return Resource(href=href, etag=_etag(data), ics_text=ics_text, uid=uid)

    def delete(self, cal_id: str, href: str, if_match: str | None = None) -> None:
        path = self._cal_dir(cal_id)
        _check_href(href)
        with self._lock(cal_id):
            f = path / href
            if not f.is_file():
                raise KeyError(f"no such resource: {href}")
            if if_match is not None and _etag(f.read_bytes()) != if_match:
                raise PreconditionFailed(f"etag mismatch on {href}")
            f.unlink()
            self._journal_append(path, href, "deleted")

    # -- sync journal ---------------------------------------------------------

    def _journal_append(self, path: Path, href: str, kind: str) -> None:
        jpath = path / ".journal.json"
        journal = _read_json(jpath)
        journal["seq"] += 1
        journal["entries"].append([journal["seq"], href, kind])
        if len(journal["entries"]) > _JOURNAL_KEEP:
            dropped = journal["entries"][: -_JOURNAL_KEEP]
            journal["entries"] = journal["entries"][-_JOURNAL_KEEP:]
            journal["floor"] = dropped[-1][0]
        _write_json(jpath, journal)

    def sync_delta(
        self, cal_id: str, token: str | None
    ) -> tuple[str, list[str], list[str]]:
        """(new_token, changed hrefs, deleted hrefs) since `token`.

        token=None means "from scratch": every current resource is changed,
        nothing is deleted.
        """
        path = self._cal_dir(cal_id)
        with self._lock(cal_id):
            journal = _read_json(path / ".journal.json")
            seq = journal["seq"]
            if token is None:
                current = sorted(f.name for f in path.glob("*.ics"))
                return str(seq), current, []
            try:
                since = int(token)
            except ValueError:
                raise StaleSyncToken(token) from None
            if since > seq or since < journal.get("floor", 0):
                raise StaleSyncToken(token)
            latest: dict[str, str] = {}
            for entry_seq, href, kind in journal["entries"]:
                if entry_seq > since:
                    latest[href] = kind
            changed = sorted(h for h, k in latest.items() if k == "changed")
            deleted = sorted(h for h, k in latest.items() if k == "deleted")
            return str(seq), changed, deleted


def _uid_of(ics_text: str) -> str:
    try:
        cal = Component.parse(ics_text)
    except ValueError:
        return ""
    for ev in cal.find_children("VEVENT"):
        uid = ev.get("UID")
        if uid is not None:
            return uid.value
    return ""


def _validate_resource(ics_text: str) -> str:
    try:
        cal = Component.parse(ics_text)
    except ValueError as exc:
        raise BadResource(f"unparseable iCalendar data: {exc}") from None
    if cal.name != "VCALENDAR":
        raise BadResource(f"top-level component is {cal.name}, not VCALENDAR")
    events = cal.find_children("VEVENT")
    if not events:
        raise BadResource("resource contains no VEVENT")
    uids = {ev.get("UID").value if ev.get("UID") else "" for ev in events}
    if len(uids) != 1 or "" in uids:
        raise BadResource(f"resource must hold exactly one UID, got {sorted(uids)}")
    return uids.pop()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)
