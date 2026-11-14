# Connecting calendar clients

Every card below needs the same three facts:

- **Base URL**: `https://your-host:5232/dav/` (or `http://` on a trusted
  network — see the README's TLS section)
- **Username / password**: what you chose in `exocalendar setup`

exocalendar implements standard CalDAV discovery (`/.well-known/caldav`,
principal and calendar-home lookup), so clients that ask for just a server
address find everything themselves.

## DAVx5 (Android)

1. Install [DAVx5](https://www.davx5.com/) (F-Droid or Play Store).
2. Add account → **Login with URL and user name** → base URL
   `https://your-host:5232/dav/`, your username and password.
3. Tick the calendars to sync. They appear in any Android calendar app.

## Apple Calendar (iOS)

1. Settings → Calendar → Accounts → Add Account → **Other** → **Add CalDAV
   Account**.
2. Server: `your-host:5232`, plus username and password. (If iOS balks at a
   port here, use Advanced Settings: port 5232, and account URL
   `https://your-host:5232/dav/u/`.)

## Apple Calendar (macOS)

1. Calendar → Settings → Accounts → **+** → Other CalDAV Account → **Advanced**.
2. Account URL `https://your-host:5232/dav/u/`, username, password.

## Thunderbird

1. Calendar → New Calendar → **On the Network**.
2. Username and location `https://your-host:5232/dav/`; Thunderbird finds the
   calendars and offers each for subscription.

## Read-only subscriptions (no password)

Any app that subscribes to an ICS URL (Google Calendar's "From URL", Outlook,
etc.) can follow a calendar read-only: in the web UI open the calendar's ⚙
settings → **Copy feed URL**. The URL embeds a secret token — treat it like a
password; reset it from the same dialog if it leaks.
