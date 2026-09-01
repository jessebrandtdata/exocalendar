# exocalendar — agent guide

Self-hostable CalDAV calendar server + web UI. **Zero runtime dependencies**
(Python 3.11+ stdlib only) — this is a hard constraint, not a preference; test
dependencies (`pytest`, `python-dateutil`, `caldav`) are fine in `[dev]`.
Nothing in the code may assume a particular host, network, or tailnet.

Spec of record: `docs/superpowers/specs/2026-08-31-exocalendar-design.md`.

## Architecture (one line each)

- `ical.py` — RFC 5545 parse/serialize; **lossless round-trip is a contract**
  (unknown props/params/components preserved byte-for-byte, order kept).
- `rrule.py` — full RFC 5545 recurrence; oracle-tested against dateutil.
  Deliberate deviations (BYDAY union semantics, ISO week numbering) are
  documented in the tests — don't "fix" them to match dateutil.
- `store.py` — one `.ics` per UID under `calendars/<id>/`; ETag/ctag/sync
  journal. Single-process locks only.
- `dav.py` — pure request→response CalDAV logic (no sockets), unit-tested
  with recorded client XML.
- `webapi.py` + `static/` — JSON API + vanilla-JS UI (no framework, no build
  step). Both the DAV layer and the web API sit on `store.py`; every edit is
  an `.ics` rewrite so clients can't disagree.
- `server.py` — stdlib HTTP shell: routing, Basic auth (`/feed/*` exempt,
  token-authed), TLS.

## Testing

```sh
python3 -m pytest                                   # full suite (~3 min)
python3 -m pytest --ignore=tests/test_rrule_oracle.py   # fast (~40 s)
EXOCAL_ORACLE_N=1200 python3 -m pytest tests/test_rrule_oracle.py  # full sweep
```

The e2e suite (`test_e2e_caldav.py`) boots the real server and drives it with
the `caldav` client library — run it after any protocol change. For UI
changes there is no committed browser test; drive the real server and check
the browser console (a scratchpad playwright script was used during
development — see the repo's PRs).

## Conventions

- TDD; every review finding lands with a regression test
  (`tests/test_review_regressions.py`).
- RFC-correctness beats dateutil-compatibility; ecosystem-compatibility beats
  both when real clients (DAVx5/Apple/Thunderbird) depend on a behavior.
- Feature branches → PRs into `dev`; `dev` → `main` PRs are the release gate.
