# exocalendar — design spec (2026-08-31)

Approved by Jesse 2026-08-31 (chat). This document is the spec of record for v1.

## What it is

A FOSS, self-hostable calendar server with a Google-Calendar-style web UI.

- **One Python package, zero runtime dependencies.** Anything Python 3.10+ runs
  it: laptop, Raspberry Pi, cheap VPS. `pip install` (or a plain clone) and
  `python -m exocalendar` is the whole install.
- **CalDAV from day one.** The CalDAV server is the core; the web UI is just
  another client of the same storage layer. Phones and desktop clients sync
  natively (DAVx5, Apple Calendar, Thunderbird).
- **Single user, built for strangers.** No assumption of any particular
  network, proxy, or host. Nothing in the code knows about tailnets or this
  box; deployment specifics live in config and unit files only.
- **MIT license.**

Explicit non-goals for v1: multi-user accounts (the DAV principal model is the
designed seam — additive later, not a redesign), CardDAV/contacts, tasks
(VTODO), email invitations/scheduling (iTIP/iMIP), server-side push.

## Architecture

```
exocalendar/
  ical.py        RFC 5545 parse/serialize, lossless round-trip
  rrule.py       full RFC 5545 recurrence expansion
  store.py       filesystem storage: calendars, events, ETags, sync tokens
  dav.py         WebDAV/CalDAV protocol handlers (XML in/out)
  webapi.py      JSON API for the web UI (same store)
  server.py      stdlib ThreadingHTTPServer, routing, Basic auth, TLS
  config.py      config.toml load + first-run interactive setup
  __main__.py    CLI: serve, adduser/passwd, import/export
  static/        web UI: vanilla JS/CSS, no build step
```

Each module has one purpose and is unit-testable without the server running.
`dav.py` and `webapi.py` both sit on `store.py`; neither knows about the other.

### ical.py — iCalendar model

- Line unfolding/folding, parameter and value parsing, escaping, per RFC 5545.
- Component tree (VCALENDAR/VEVENT/VTIMEZONE/...); typed accessors for the
  properties we interpret (DTSTART, DTEND/DURATION, RRULE, EXDATE, RDATE,
  RECURRENCE-ID, UID, SUMMARY, ...).
- **Lossless round-trip is a hard requirement:** unknown properties,
  parameters, and components are preserved byte-equivalent (modulo line
  folding), so foreign clients' data is never mangled. Property order is
  preserved.
- Date/time handling: DATE vs DATE-TIME, floating vs UTC vs TZID. VTIMEZONE
  components are parsed and used for TZID resolution (fall back to the host
  zoneinfo database via stdlib `zoneinfo` when a TZID has no VTIMEZONE).

### rrule.py — recurrence engine

- Full RFC 5545: FREQ (all), INTERVAL, COUNT, UNTIL, BYSECOND/MINUTE/HOUR,
  BYDAY (with ordinals), BYMONTHDAY, BYYEARDAY, BYWEEKNO, BYMONTH, BYSETPOS,
  WKST. Plus EXDATE, RDATE, and RECURRENCE-ID overrides (modified/cancelled
  single occurrences), which are resolved at the occurrence-expansion layer.
- Iterator API: `expand(event, range_start, range_end) -> occurrences`, lazy,
  with a hard cap on iterations to bound pathological rules.
- **Correctness strategy:** hand-rolled (zero runtime deps) but oracle-tested
  against `python-dateutil` (dev dependency only): a large corpus of
  hand-picked spec examples + thousands of property-based random rules; both
  engines must agree on the first N occurrences.

### store.py — storage

Radicale-style filesystem layout, human-inspectable and backup-friendly:

```
<data_dir>/
  calendars/<calendar-id>/
    .props.json          displayname, color, description, order
    <uid>.ics            one resource (event + its overrides) per file
  tokens.json            sync-token journal per calendar
```

- ETag = content hash. Calendar ctag = hash over member ETags.
- sync-collection support requires remembering deletions: per-calendar
  journal of (seq, href, changed|deleted); sync-token = seq number. Journal
  is pruned past a retention window; a too-old token gets 410 per spec, and
  clients re-fetch.
- Concurrency: process-wide per-calendar locks; atomic writes
  (tmp + rename). Single-process server, so no cross-process locking in v1.

### dav.py — CalDAV

- Methods: OPTIONS (DAV: 1, calendar-access), PROPFIND (depth 0/1),
  PROPPATCH (displayname/color), MKCALENDAR, GET/HEAD, PUT (with
  If-Match/If-None-Match), DELETE, REPORT.
- REPORTs: calendar-query (with time-range filter — requires RRULE expansion
  to decide membership), calendar-multiget, sync-collection.
- Discovery: `/.well-known/caldav` redirect, current-user-principal,
  calendar-home-set. Paths: `/dav/` principal at `/dav/u/`, calendars under
  `/dav/u/<calendar-id>/`.
- XML via stdlib `xml.etree`; responses built with correct namespaces
  (DAV:, urn:ietf:params:xml:ns:caldav, and the apple-ical namespace for
  calendar-color).
- Compatibility targets in priority order: DAVx5, Apple Calendar
  (iOS/macOS), Thunderbird. Their quirks get golden request/response tests.

### webapi.py + static/ — web UI

- JSON API: list calendars, occurrences-in-range (server expands recurrence),
  CRUD events, create/edit "this occurrence / this and future / all",
  import .ics file, export calendar as .ics.
- Read-only ICS feed: `GET /feed/<calendar-id>.ics` with a per-calendar
  secret token in the URL (regenerable), so external apps can subscribe
  without credentials.
- UI: month / week / day views; click-drag create; drag to move, edge-drag to
  resize; event editor with full recurrence builder (frequency, interval,
  weekday picks, monthly patterns, until/count) plus a raw-RRULE escape hatch;
  multiple calendars with show/hide and colors (ColorBrewer Set2-derived
  palette, never default-rainbow); ICS import/export buttons. Keyboard: t
  (today), arrows (prev/next period), m/w/d (views).
- Vanilla JS + CSS, one page, no framework, no build step. Light/dark via
  prefers-color-scheme.

### server.py / config.py / auth

- stdlib `ThreadingHTTPServer`. Routing: `/dav/*` and `/.well-known/caldav`
  → dav; `/api/*` → webapi; `/feed/*` → feeds; everything else → static UI.
- HTTP Basic auth on everything except `/feed/*` (token-authed); credentials
  from config: single username + salted PBKDF2 password hash (stdlib
  `hashlib`). Web UI uses the same Basic auth (browser-native prompt) — no
  session layer in v1.
- `--no-auth` flag: only takes effect when bound to a loopback address; the
  server refuses no-auth on non-loopback binds.
- Optional TLS: cert/key paths in config, stdlib `ssl`. README documents the
  reverse-proxy alternative. Plain HTTP is allowed but warned about at
  startup on non-loopback binds (Basic auth over cleartext).
- Config: `config.toml` (stdlib `tomllib`; written by a small emitter) —
  username, password_hash, bind, port, data_dir, tls cert/key. Default
  location `~/.config/exocalendar/config.toml`, `--config` to override.
  First run with no config prompts interactively (or `exocalendar setup`)
  and writes the file; config stays hand-editable.
- CLI (`python -m exocalendar` / console script `exocalendar`): `serve`,
  `setup`, `passwd`, `import <file.ics> [--calendar id]`,
  `export <calendar-id>`.

## Testing & verification

- pytest + dev-deps only (`pytest`, `python-dateutil` as the RRULE oracle,
  `caldav` as an e2e client). Runtime deps remain zero.
- Unit: ical round-trip corpus (including files produced by Google Calendar,
  Apple, Outlook exports), RRULE oracle + spec-example suite, store ETag and
  sync-journal semantics, config round-trip.
- Protocol goldens: recorded request/response pairs for the discovery and
  sync flows the target clients actually perform.
- End-to-end: boot the real server on a random port, drive it with the
  `caldav` library — discover, create calendar, create/edit/delete events,
  recurring events with overrides, incremental sync — over the wire.
- Manual (Jesse, ~3 minutes, at the end): point DAVx5 or Apple Calendar at
  the server with the setup card in the README; that's the only human step.

## Repo & delivery

- Repo `jessebrandtdata/exocalendar`, MIT. Dev in `~/dev/exocalendar`.
- Phases, each a feature PR into `dev` (self-reviewed and merged per the
  aggregation flow), one `dev` → `main` PR for Jesse at the end:
  1. Scaffold + `ical.py` (parse/serialize, round-trip corpus)
  2. `rrule.py` (full RFC 5545 + oracle harness)
  3. `store.py` + `dav.py` + `server.py`/auth/config (syncable server)
  4. Web UI (`webapi.py` + `static/`)
  5. Packaging, README/docs, `exocalendar.service` + `deploy-hook`
     (installable-contract files for boxes that want them; the app itself
     stays host-agnostic), e2e suite polish
- Palace install (manifest row via exolaunch) happens only after merge, as
  Jesse's click.
