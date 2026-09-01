# exocalendar

A self-hostable calendar server with a clean web UI and **native CalDAV
sync** — your phone's calendar app, Apple Calendar, and Thunderbird all speak
to it directly. One Python package, **zero runtime dependencies**: anything
with Python 3.11+ runs it — a laptop, a Raspberry Pi, a $3 VPS.

- 📅 Google-Calendar-style web UI: month/week/day, drag to create/move/resize,
  full recurring-event support ("this event / this and following / all")
- 🔁 Full RFC 5545 recurrence engine (every RRULE the spec allows), tested
  against python-dateutil on thousands of generated rules
- 📱 CalDAV server: sync natively with DAVx5 (Android), iOS/macOS Calendar,
  Thunderbird — including efficient incremental sync (RFC 6578)
- 📥 ICS import/export, plus per-calendar read-only feed URLs any app can
  subscribe to
- 🗂 Your data is plain `.ics` files in a directory you own. Back it up with
  `rsync`; read it with anything.
- 🔒 Single user, HTTP Basic auth (PBKDF2-hashed password), optional built-in
  TLS. Multi-user is a designed-for later, not a v1 feature.

## Install

```sh
pip install --user git+https://github.com/jessebrandtdata/exocalendar
exocalendar setup     # asks for username/password/port, writes config.toml
exocalendar serve
```

Or from a clone: `python3 -m exocalendar serve` (no build step, no
dependencies to install).

Open `http://localhost:5232/` for the web UI. The config lives at
`~/.config/exocalendar/config.toml` and stays hand-editable; events live under
`~/.local/share/exocalendar/` (both configurable).

## Sync your devices

Point any CalDAV client at your server — see **[docs/CLIENTS.md](docs/CLIENTS.md)**
for 3-line setup cards for DAVx5, Apple Calendar, and Thunderbird. The short
version: base URL `http://your-host:5232/dav/`, plus your username and
password.

## Exposing it beyond localhost

exocalendar binds to `127.0.0.1` by default. To reach it from other devices,
set `bind = "0.0.0.0"` (or your LAN/VPN address) in the config — and put TLS
in front, because Basic auth over plain HTTP is readable in transit:

- built-in: set `tls_cert` / `tls_key` in the config (any PEM pair, e.g. from
  Let's Encrypt or `openssl req -x509 ...` for a self-signed lab cert), or
- a reverse proxy (Caddy, nginx) terminating TLS in front of the plain port.

The server warns at startup if it is serving plain HTTP beyond loopback.
`exocalendar serve --no-auth` exists for local trials and refuses to run on
non-loopback binds.

## CLI

```
exocalendar serve [--bind ADDR] [--port N] [--no-auth]
exocalendar setup                 # interactive first-run config
exocalendar passwd                # change the password
exocalendar import FILE --calendar ID
exocalendar export ID > backup.ics
```

## Data layout

```
<data_dir>/calendars/<calendar-id>/
    .props.json    display name, color, feed token
    .journal.json  sync journal (incremental-sync bookkeeping)
    <uid>.ics      one file per event (with its recurrence overrides)
```

Every file is standard iCalendar; unknown properties from other clients are
preserved byte-for-byte. Backup = copy the directory.

## Development

```sh
pip install --user -e ".[dev]"    # pytest, python-dateutil, caldav (test-only)
python3 -m pytest
```

The test suite includes an RRULE oracle (thousands of random rules compared
against python-dateutil), protocol tests for every DAV method, and an
end-to-end suite that boots the real server and syncs against the `caldav`
client library. `EXOCAL_ORACLE_N=1200 python3 -m pytest tests/test_rrule_oracle.py`
runs the full oracle sweep.

Known v1 limits: one user; `RANGE=THISANDFUTURE` recurrence overrides from
other clients are treated as single-instance overrides (the web UI's own
"this and following" edits use series splits, which every client understands);
VTODO/VJOURNAL components are stored and served but not shown in the UI.

## License

MIT.
