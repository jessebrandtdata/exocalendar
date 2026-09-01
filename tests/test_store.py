import threading

import pytest

from exocalendar.store import (
    BadResource,
    PreconditionFailed,
    StaleSyncToken,
    Store,
)

ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//exocalendar//EN\r
BEGIN:VEVENT\r
UID:{uid}\r
DTSTART:20260601T100000Z\r
DTEND:20260601T110000Z\r
SUMMARY:{summary}\r
END:VEVENT\r
END:VCALENDAR\r
"""


def ics(uid="u1", summary="Hello"):
    return ICS.format(uid=uid, summary=summary)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def test_create_and_list_calendars(store):
    info = store.create_calendar("work", "Work", "#1b9e77")
    assert info.id == "work"
    assert info.displayname == "Work"
    assert info.color == "#1b9e77"
    assert info.ctag
    assert info.feed_token
    (got,) = store.list_calendars()
    assert got.id == "work"


def test_create_duplicate_calendar_raises(store):
    store.create_calendar("work", "Work", "#1b9e77")
    with pytest.raises(ValueError):
        store.create_calendar("work", "Again", "#000000")


def test_calendar_id_sanitized(store):
    for bad in ("../oops", "a/b", ".hidden", "", "a" * 100):
        with pytest.raises(ValueError):
            store.create_calendar(bad, "X", "#000000")


def test_update_calendar_props(store):
    store.create_calendar("work", "Work", "#1b9e77")
    store.update_calendar_props("work", displayname="Job", color="#d95f02")
    (got,) = store.list_calendars()
    assert got.displayname == "Job"
    assert got.color == "#d95f02"


def test_delete_calendar(store):
    store.create_calendar("work", "Work", "#1b9e77")
    store.put("work", "u1.ics", ics())
    store.delete_calendar("work")
    assert store.list_calendars() == []
    assert store.get_calendar("work") is None


def test_put_get_roundtrip(store):
    store.create_calendar("cal", "Cal", "#111111")
    res = store.put("cal", "u1.ics", ics())
    assert res.href == "u1.ics"
    assert res.uid == "u1"
    assert res.etag
    got = store.get("cal", "u1.ics")
    assert got.ics_text == ics()
    assert got.etag == res.etag
    assert store.get("cal", "missing.ics") is None


def test_etag_changes_with_content(store):
    store.create_calendar("cal", "Cal", "#111111")
    e1 = store.put("cal", "u1.ics", ics()).etag
    e2 = store.put("cal", "u1.ics", ics(summary="Changed")).etag
    assert e1 != e2
    e3 = store.put("cal", "u1.ics", ics(summary="Changed")).etag
    assert e2 == e3


def test_ctag_changes_on_writes(store):
    store.create_calendar("cal", "Cal", "#111111")
    c0 = store.get_calendar("cal").ctag
    store.put("cal", "u1.ics", ics())
    c1 = store.get_calendar("cal").ctag
    assert c0 != c1
    store.delete("cal", "u1.ics")
    c2 = store.get_calendar("cal").ctag
    assert c2 not in (c0, c1) or c2 == c0  # deletion changes it again


def test_preconditions(store):
    store.create_calendar("cal", "Cal", "#111111")
    res = store.put("cal", "u1.ics", ics())
    # If-None-Match * on existing resource
    with pytest.raises(PreconditionFailed):
        store.put("cal", "u1.ics", ics(), if_none_match=True)
    # If-Match with wrong etag
    with pytest.raises(PreconditionFailed):
        store.put("cal", "u1.ics", ics(summary="X"), if_match="wrong")
    # If-Match with right etag succeeds
    store.put("cal", "u1.ics", ics(summary="X"), if_match=res.etag)
    # delete with wrong etag
    with pytest.raises(PreconditionFailed):
        store.delete("cal", "u1.ics", if_match="nope")
    # If-Match on a missing resource
    with pytest.raises(PreconditionFailed):
        store.put("cal", "new.ics", ics(uid="new"), if_match=res.etag)


def test_put_validates_ics(store):
    store.create_calendar("cal", "Cal", "#111111")
    with pytest.raises(BadResource):
        store.put("cal", "u1.ics", "not an ics")
    with pytest.raises(BadResource):
        store.put("cal", "u1.ics", "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")
    # UID mismatch across VEVENTs in one resource
    two_uids = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:a\r\nDTSTART:20260601T100000Z\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b\r\nDTSTART:20260602T100000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    with pytest.raises(BadResource):
        store.put("cal", "a.ics", two_uids)
    # changing the UID of an existing resource
    store.put("cal", "u1.ics", ics(uid="u1"))
    with pytest.raises(BadResource):
        store.put("cal", "u1.ics", ics(uid="other"))


def test_put_to_missing_calendar(store):
    with pytest.raises(KeyError):
        store.put("nope", "u1.ics", ics())


def test_list_resources(store):
    store.create_calendar("cal", "Cal", "#111111")
    store.put("cal", "u1.ics", ics(uid="u1"))
    store.put("cal", "u2.ics", ics(uid="u2"))
    hrefs = sorted(r.href for r in store.list_resources("cal"))
    assert hrefs == ["u1.ics", "u2.ics"]


def test_delete(store):
    store.create_calendar("cal", "Cal", "#111111")
    store.put("cal", "u1.ics", ics())
    store.delete("cal", "u1.ics")
    assert store.get("cal", "u1.ics") is None
    with pytest.raises(KeyError):
        store.delete("cal", "u1.ics")


def test_sync_delta_full_and_incremental(store):
    store.create_calendar("cal", "Cal", "#111111")
    token0, changed, deleted = store.sync_delta("cal", None)
    assert changed == [] and deleted == []
    store.put("cal", "u1.ics", ics(uid="u1"))
    store.put("cal", "u2.ics", ics(uid="u2"))
    token1, changed, deleted = store.sync_delta("cal", token0)
    assert sorted(changed) == ["u1.ics", "u2.ics"] and deleted == []
    store.put("cal", "u1.ics", ics(uid="u1", summary="edit"))
    store.delete("cal", "u2.ics")
    token2, changed, deleted = store.sync_delta("cal", token1)
    assert changed == ["u1.ics"] and deleted == ["u2.ics"]
    # no changes -> empty delta, token stable
    token3, changed, deleted = store.sync_delta("cal", token2)
    assert changed == [] and deleted == []
    # full listing from None includes current resources only
    _tok, changed, deleted = store.sync_delta("cal", None)
    assert changed == ["u1.ics"] and deleted == []


def test_sync_delta_stale_token(store):
    store.create_calendar("cal", "Cal", "#111111")
    with pytest.raises(StaleSyncToken):
        store.sync_delta("cal", "garbage")
    with pytest.raises(StaleSyncToken):
        store.sync_delta("cal", "999999")


def test_sync_token_survives_reopen(store, tmp_path):
    store.create_calendar("cal", "Cal", "#111111")
    tok, _, _ = store.sync_delta("cal", None)
    store.put("cal", "u1.ics", ics())
    reopened = Store(tmp_path / "data")
    _tok2, changed, deleted = reopened.sync_delta("cal", tok)
    assert changed == ["u1.ics"]


def test_props_survive_reopen(store, tmp_path):
    store.create_calendar("cal", "My Cal", "#123456")
    reopened = Store(tmp_path / "data")
    got = reopened.get_calendar("cal")
    assert got.displayname == "My Cal"
    assert got.color == "#123456"
    assert got.feed_token == store.get_calendar("cal").feed_token


def test_concurrent_puts_all_land(store):
    store.create_calendar("cal", "Cal", "#111111")
    errors = []

    def worker(i):
        try:
            store.put("cal", f"u{i}.ics", ics(uid=f"u{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.list_resources("cal")) == 20
    # journal recorded every write
    _tok, changed, _deleted = store.sync_delta("cal", None)
    assert len(changed) == 20


def test_href_sanitized(store):
    store.create_calendar("cal", "Cal", "#111111")
    for bad in ("../x.ics", "a/b.ics", "", ".hidden.ics"):
        with pytest.raises(ValueError):
            store.put("cal", bad, ics())
