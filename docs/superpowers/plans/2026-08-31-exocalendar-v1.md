# exocalendar v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A zero-runtime-dependency, self-hostable CalDAV calendar server with a Google-Calendar-style web UI, installable anywhere Python 3.10+ runs.

**Architecture:** Filesystem-backed store (one .ics per event resource); CalDAV handlers and a JSON web API both sit on the store; a stdlib ThreadingHTTPServer routes `/dav`, `/api`, `/feed`, and static UI; full RFC 5545 recurrence engine oracle-tested against python-dateutil.

**Tech Stack:** Python 3.10+ stdlib only at runtime. Dev deps: pytest, python-dateutil (RRULE oracle), caldav (e2e client). Vanilla JS/CSS frontend, no build step.

**Spec:** docs/superpowers/specs/2026-08-31-exocalendar-design.md

## Global Constraints

- Zero runtime dependencies; stdlib only (`zoneinfo`, `tomllib`, `xml.etree`, `hashlib`, `ssl`, `http.server`).
- Python floor: 3.10 (`tomllib` fallback: vendor a minimal reader? No — floor is 3.11 where tomllib landed. **Floor = 3.11.**)
- Lossless iCalendar round-trip: unknown properties/params/components preserved, property order preserved.
- Nothing tailnet- or host-aware in code; deployment specifics live in config/unit files.
- MIT license. Repo `jessebrandtdata/exocalendar`. Feature PRs → `dev` (self-merged), one `dev`→`main` PR for Jesse.
- No hardcoded personal identity in code; single principal is named `u`.
- TDD per task; commit at each green step.

---

### Task 1: Scaffold

**Files:** Create `pyproject.toml`, `LICENSE` (MIT), `.gitignore`, `README.md` (stub), `exocalendar/__init__.py` (`__version__ = "0.1.0"`), `tests/test_smoke.py`.

**Interfaces produced:** package `exocalendar` importable; `pytest` runs; console script `exocalendar = exocalendar.__main__:main` declared (module added in Task 8).

- [ ] pyproject: `[project] name="exocalendar" requires-python=">=3.11" dependencies=[]`; `[project.optional-dependencies] dev=["pytest","python-dateutil","caldav"]`; setuptools backend, packages include `exocalendar*`, `exocalendar.static` as package data.
- [ ] Smoke test imports package, asserts version. Run pytest → green. Commit.
- [ ] Create GitHub repo `jessebrandtdata/exocalendar` (public, MIT), push `main`, create `dev` branch, push. All subsequent tasks on feature branches off `dev`.

### Task 2: ical.py — lines and components

**Files:** Create `exocalendar/ical.py`, `tests/test_ical.py`, `tests/corpus/*.ics`.

**Interfaces produced:**
- `unfold(text: str) -> list[str]`; `fold_line(line: str) -> str` (75-octet folding, UTF-8 safe).
- `ContentLine` dataclass: `name: str`, `params: list[tuple[str, list[str]]]`, `value: str` (raw/escaped form); `ContentLine.parse(line) -> ContentLine`; `.serialize() -> str` (unfolded); `.param(name) -> str|None`; value escaping helpers `escape_text`/`unescape_text`.
- `Component`: `name: str`, `lines: list[ContentLine]`, `children: list[Component]`; `Component.parse(text) -> Component` (returns the single top component; multi-VCALENDAR input → wrapper handling in caller), `parse_all(text) -> list[Component]`; `.serialize() -> str` (CRLF, folded); `.get(name)`, `.get_all(name)`, `.set(name, value, params=[])`, `.remove(name)`; `.find_children(name)`.

**Tests (concrete):**
- Fold/unfold round-trip incl. a 200-char summary with multibyte chars (no split inside a UTF-8 sequence).
- Param parsing: `ATTENDEE;CN="Doe, John";ROLE=REQ-PARTICIPANT:mailto:x@y.z` → CN keeps comma; quoted-string params round-trip.
- Escapes: `SUMMARY:a\, b\n c\\d` → value `a, b\n c\d` and back.
- Round-trip byte-equivalence (modulo folding + CRLF normalization) on corpus files: a Google Calendar export, an Apple export, an Outlook invite, RFC 5545 examples — unknown `X-` properties and VALARM children preserved in order.

- [ ] Write failing tests → implement → green → commit (repeat per unit above).

### Task 3: ical.py — dates, times, timezones

**Files:** Modify `exocalendar/ical.py`; test `tests/test_ical_dt.py`.

**Interfaces produced:**
- `TZResolver`: built from a VCALENDAR's VTIMEZONEs; `resolve(tzid) -> tzinfo` (VTIMEZONE-defined custom zone → stdlib `zoneinfo.ZoneInfo(tzid)` fallback → raise `TZUnknown`). VTIMEZONE interpretation: STANDARD/DAYLIGHT with DTSTART, TZOFFSETFROM/TO, RRULE (yearly BYDAY/BYMONTH forms) — implemented as a small fixed-offset-rule tzinfo subclass.
- `parse_dt(prop: ContentLine, tz: TZResolver) -> DTValue` where `DTValue = dataclass(value: date|datetime, is_date: bool, tzid: str|None)`; UTC `Z` forms → aware UTC; floating → naive.
- `serialize_dt(dtv) -> (value_str, params)`.
- `parse_duration(text) -> timedelta` and inverse (RFC 5545 dur-value incl. weeks, negative).
- `event_span(vevent, tz) -> (DTValue start, DTValue end)`: DTEND, else DTSTART+DURATION, else RFC 5545 defaults (date → +1 day, datetime → zero length).

**Tests:** `DTSTART;TZID=America/New_York:20260308T013000` around the DST gap; `VALUE=DATE`; `Z` forms; custom VTIMEZONE with non-Olson TZID resolves via its own rules; `-P1DT12H` duration; event_span defaults per spec.

### Task 4: rrule.py — full recurrence engine

**Files:** Create `exocalendar/rrule.py`, `tests/test_rrule.py`, `tests/test_rrule_oracle.py`.

**Interfaces produced:**
- `RRule.parse(text: str) -> RRule` (validates parts, raises `RRuleError` on garbage; unknown parts rejected).
- `RRule.iterate(dtstart: date|datetime) -> Iterator[...]` — lazy, correct RFC 5545 semantics: expansion vs limitation per FREQ per the RFC table, BYSETPOS applied per interval set, WKST for weekly/BYWEEKNO, COUNT/UNTIL (UNTIL compared in UTC for aware dtstart), DTSTART always first occurrence unless filtered per RFC. Hard cap `MAX_ITERATIONS = 100_000` empty-interval scans → `RRuleOverflow`.
- `expand(vevent_set: list[Component], tz: TZResolver, range_start: datetime, range_end: datetime, limit: int = 10_000) -> list[Occurrence]` — takes master + RECURRENCE-ID overrides sharing a UID; applies RDATE (incl. PERIOD form), EXDATE, override replacement (incl. overrides that move an occurrence into/out of range), cancelled overrides (STATUS:CANCELLED); `Occurrence = dataclass(uid, recurrence_id: DTValue|None, start, end, component)`. Non-recurring events yield one occurrence if they intersect the range.
- `intersects(occ, range) -> bool` uses half-open [start, end).

**Tests (concrete):**
- Every RRULE example from RFC 5545 §3.8.5.3 with its listed expected dates (the RFC prints expected outputs — encode them all, including the BYSETPOS "last work day of month" and BYWEEKNO cases and the invalid Feb-30 skip).
- Oracle: property-based generator producing ~3000 random valid RRULEs (random FREQ, INTERVAL 1–4, random BY* subsets valid for the FREQ, COUNT≤100) × random DTSTARTs; assert first 40 occurrences equal `dateutil.rrule.rrulestr` output. Documented, pinned seed. Known dateutil spec deviations, if hit, get case-by-case adjudication comments citing the RFC.
- EXDATE/RDATE/override tests: move one occurrence, cancel one, override with different duration.

### Task 5: store.py

**Files:** Create `exocalendar/store.py`, `tests/test_store.py`.

**Interfaces produced:**
- `Store(data_dir: Path)`; layout per spec (`calendars/<id>/<uid>.ics`, `.props.json`, `tokens.json`).
- Calendars: `list_calendars() -> list[CalInfo]`, `create_calendar(cal_id, displayname, color) -> CalInfo`, `update_calendar_props(cal_id, **props)`, `delete_calendar(cal_id)`; `CalInfo = dataclass(id, displayname, color, description, order, ctag)`.
- Resources: `list_resources(cal_id) -> list[Resource]`, `get(cal_id, href) -> Resource|None`, `put(cal_id, href, ics_text, *, if_match: str|None = None, if_none_match: bool = False) -> Resource` (raises `PreconditionFailed`), `delete(cal_id, href, if_match=None)`; `Resource = dataclass(href, etag, ics_text, uid)`. href is the filename (`<uid>.ics`, uid percent-safe-sanitized); ETag = sha256 of bytes; ctag = sha256 over sorted (href, etag).
- Sync: every put/delete appends `(seq, href, "changed"|"deleted")` to the calendar's journal in `tokens.json`; `sync_delta(cal_id, token: str|None) -> (new_token, changed: list[href], deleted: list[href])`; unknown/pruned token → `StaleSyncToken`. Journal pruned to last 5000 entries.
- Concurrency: module-level `threading.Lock` per calendar id; writes are tmp+`os.replace`.
- Validation on put: parseable VCALENDAR, ≥1 VEVENT, all VEVENTs share one UID, UID matches existing resource's UID on overwrite.

**Tests:** CRUD round-trip; etag changes on rewrite, stable on identical content; precondition failures; sync_delta across create/modify/delete incl. token=None full listing; stale token raises; props persist; calendar ids sanitized (reject `../`).

### Task 6: dav.py + server.py + config.py + auth

**Files:** Create `exocalendar/dav.py`, `exocalendar/server.py`, `exocalendar/config.py`, `exocalendar/auth.py`; tests `tests/test_dav.py`, `tests/test_config.py`, `tests/test_server_routing.py`.

**Interfaces produced:**
- `config.py`: `Config = dataclass(username, password_hash, bind="127.0.0.1", port=5232, data_dir, tls_cert=None, tls_key=None)`; `load(path) -> Config` (tomllib), `save(path, cfg)` (hand emitter), `interactive_setup(path) -> Config`; default path `~/.config/exocalendar/config.toml` (XDG_CONFIG_HOME respected).
- `auth.py`: `hash_password(pw) -> str` (`pbkdf2$sha256$600000$<salt_b64>$<hash_b64>`), `verify(pw, stored) -> bool` (constant-time), `check_basic(header_value, cfg) -> bool`.
- `dav.py`: `class DavHandlerLogic` (pure: takes method, path, headers, body bytes, `Store`; returns `(status, headers, body)`) — OPTIONS (`DAV: 1, 3, calendar-access`), PROPFIND depth 0/1 on principal/home/calendar/resource (props: resourcetype, displayname, getetag, getctag [http://calendarserver.org/ns/], calendar-color [http://apple.com/ns/ical/], supported-calendar-component-set, current-user-principal, calendar-home-set, sync-token, supported-report-set, owner, getcontenttype), PROPPATCH (displayname, calendar-color), MKCALENDAR, MKCOL(calendar), GET/HEAD/PUT/DELETE with ETag preconditions mapped onto Store, REPORT calendar-query (comp-filter VCALENDAR/VEVENT + time-range via `rrule.expand` membership; unsupported filters → full set fallback is NOT allowed, return only matching), calendar-multiget, sync-collection (Store.sync_delta; stale → 410 + valid-sync-token error body). 207 multistatus XML via `xml.etree` with registered namespaces. Paths: `/dav/` → 301 context, principal `/dav/u/`, home `/dav/u/`, calendars `/dav/u/<cal>/`, resources `/dav/u/<cal>/<href>`. `/.well-known/caldav` → 301 `/dav/`.
- `server.py`: `ThreadingHTTPServer` + `BaseHTTPRequestHandler` subclass routing `/dav|/.well-known/caldav` → dav, `/api/` → webapi (Task 7), `/feed/` → feeds (Task 7), else static; Basic-auth gate on everything except `/feed/`; `WWW-Authenticate: Basic realm="exocalendar"`; `--no-auth` honored only for loopback binds (refuse otherwise at startup); optional `ssl.SSLContext` wrap; startup warning for non-loopback plain HTTP; `serve(cfg, no_auth=False)`.

**Tests:** config round-trip incl. save→load equality; password hash verify/negative; DavHandlerLogic driven directly with recorded XML bodies: discovery chain (well-known → principal → home → calendar list) as DAVx5 performs it, calendar-query with time-range returning a recurring event whose master DTSTART is outside the range but an occurrence inside, multiget, full sync-collection cycle (initial → change → delta → delete → delta → stale token → 410), PUT If-None-Match `*` conflict → 412, MKCALENDAR then PROPFIND shows it. Routing tests: auth required on /dav and /api, not on /feed; no-auth refused on 0.0.0.0.

### Task 7: webapi.py + static UI + feeds

**Files:** Create `exocalendar/webapi.py`, `exocalendar/static/` (`index.html`, `app.js`, `style.css`), feed handling in `webapi.py`; tests `tests/test_webapi.py`.

**Interfaces produced (JSON API, all under `/api`):**
- `GET /api/calendars` → `[{id, displayname, color, order, feed_token}]`; `POST /api/calendars {id?, displayname, color}`; `PATCH /api/calendars/<id>`; `DELETE /api/calendars/<id>`; `POST /api/calendars/<id>/rotate-feed-token`.
- `GET /api/occurrences?start=<iso>&end=<iso>&calendars=a,b` → `[{cal, href, uid, recurrence_id, start, end, all_day, summary, location, description, rrule_text, is_recurring, color}]` (server expands; times ISO 8601 with offsets, all-day as dates).
- `POST /api/events {cal, summary, start, end, all_day, tzid, location?, description?, rrule?}` → creates VEVENT (new UID via `uuid4`), returns `{href, etag}`.
- `PUT /api/events/<cal>/<href>` body includes `etag` + `scope: "all"|"this"|"future"` + `recurrence_id?` + fields — scope `this` writes a RECURRENCE-ID override into the resource; `future` splits: master gets UNTIL just before recurrence_id, new resource created from recurrence_id onward; `all` edits master (start delta applied to DTSTART).
- `DELETE /api/events/<cal>/<href>` with `scope`/`recurrence_id`: `this` → add EXDATE; `future` → set UNTIL; `all` → delete resource.
- `POST /api/import?calendar=<id>` (body = .ics file) → splits by UID into resources, returns counts; `GET /api/export/<cal>.ics` → whole calendar as one VCALENDAR.
- Feeds: `GET /feed/<cal>.ics?t=<token>` — token from `.props.json` (`secrets.token_urlsafe(16)`, created on calendar creation), read-only, no Basic auth, wrong token → 404.
- Errors: JSON `{error}`, 409 on etag mismatch, 400 validation.

**UI (vanilla, one page):** month/week/day grids rendered from `/api/occurrences`; header: prev/next/today, view switcher, calendar checklist with colors (Set2-derived palette for new calendars), import button (file input → POST), export/feed-URL per calendar; click empty slot → editor modal (drag-create on week/day); drag move + edge-resize in week/day, drag between days in month; editor: summary, calendar, all-day, start/end with tz, location, description, recurrence builder (none/daily/weekly+weekday-picks/monthly by-date|by-weekday-ordinal/yearly, interval, ends never/on-date/after-N) + raw RRULE field kept in sync, and on saving edits to a recurring event a this/future/all chooser; delete likewise. Keyboard: t/arrows/m/w/d. `prefers-color-scheme` light/dark. Recurring events whose RRULE the builder can't represent show the raw RRULE read-only (still editable as text).

**Tests:** occurrence range query incl. recurring + all-day spanning boundaries; create→edit(this)→edit(future)→delete(this) sequence verifying resulting ICS (override present, UNTIL set, EXDATE added); import splits UIDs, export merges; feed token auth; etag conflict 409. UI is exercised by e2e (Task 8) plus hand-check.

### Task 8: CLI, packaging, e2e, docs, deploy files

**Files:** Create `exocalendar/__main__.py`, `tests/test_e2e_caldav.py`, `README.md` (real), `docs/CLIENTS.md` (DAVx5/Apple/Thunderbird setup cards), `exocalendar.service`, `deploy-hook`, `CLAUDE.md`.

**Interfaces produced:**
- CLI: `exocalendar serve [--config PATH] [--no-auth] [--bind] [--port]`, `exocalendar setup`, `exocalendar passwd`, `exocalendar import FILE --calendar ID`, `exocalendar export ID`; `serve` with no config → runs `interactive_setup` when tty, errors with pointer otherwise.
- e2e: pytest fixture boots `serve()` on 127.0.0.1 random port in a thread with a tmp data dir + test credentials; `caldav` client: discovers principal, lists calendars, creates calendar, adds event, adds recurring event, date-searches (time-range), edits, deletes, and a second client instance performs sync-collection-based incremental fetch. Marked `@pytest.mark.e2e`, runs in default suite.
- `exocalendar.service`: user unit, `ExecStart=%h/.local/bin/exocalendar serve` with absolute-path note (house: %h doesn't expand in some fields — use ExecStart absolute), `Restart=on-failure`; deploy-hook: `python3 -m pip install --user --break-system-packages -e . && pytest -q` guarded to house PATH quirks. These files serve the installable contract; README states the app itself is host-agnostic.
- README for strangers: what it is, 3-line install, first-run setup, client setup pointer, backup story (rsync the data dir), TLS/reverse-proxy note, no-auth loopback mode, license.

- [ ] Final: full `pytest` green; self-review diff; merge accumulated feature PRs; open `dev` → `main` PR for Jesse; arm PR watcher.

## Self-review notes

- Spec coverage: every spec section maps to a task (ical→2/3, rrule→4, store→5, dav/server/config/auth→6, webapi/UI/feeds→7, CLI/packaging/e2e/docs/deploy→8, scaffold→1). Python floor corrected to 3.11 (tomllib).
- Type consistency: `Store` API names used by Task 6/7 match Task 5; `rrule.expand` signature used by dav calendar-query and webapi occurrences matches Task 4; `DTValue` shared from Task 3.
